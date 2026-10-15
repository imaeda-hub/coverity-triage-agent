// dynamic workflow coverity-triage-run（GitHub Copilot CLI の拡張機能。実験的な機能）
//
// プラグインの com.github.copilot/extensions/ に置いた拡張機能は、プラグインを入れると Copilot CLI が読み込む
// （Copilot CLI 1.0.79 の変更履歴）。流れの図は docs/dynamic-workflow.md にある。
// コメントの [S1] などは、その図の箱の ID。図を直したら、このファイルも合わせて直す。

import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineWorkflow, joinSession } from "@github/copilot-sdk/extension";

// このファイルは <プラグイン>/com.github.copilot/extensions/coverity-triage-run/ にある。
const PLUGIN_ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const CT_PY = `${PLUGIN_ROOT}skills/coverity-triage-scripts/scripts/ct.py`;
const SERVER_DIR = `${PLUGIN_ROOT}server`;

const AGENTS = {
    investigator: "coverity-triage-investigator",
    fixer: "coverity-triage-fixer",
    deviation: "coverity-triage-deviation",
    reviewer: "coverity-triage-reviewer",
};
const MAX_REDO = 2;

const PHASES = [
    { title: "準備", detail: "新しい実行を作る、または前回の実行を開く" },
    { title: "検索", detail: "Coverity で警告を探す" },
    { title: "計画", detail: "作業の一覧を作る" },
    { title: "調査", detail: "作業を 1 件ずつ調べる" },
    { title: "検証", detail: "修正案をまとめてビルドで確かめる（設定したときだけ）" },
    { title: "一覧", detail: "一覧 summary.md を作る" },
];

// サブエージェントの答えの形（triage-report の result.json の各部分）。
const TEXT = { type: "string" };
const TEXTS = { type: "array", items: TEXT };
const SCHEMAS = {
    investigator: {
        type: "object",
        required: ["verdict", "recommendation", "confidence", "confidence_reason", "revision_drift",
            "group_excluded_cids"],
        properties: {
            verdict: {
                type: "object",
                required: ["judgement", "summary", "rationale", "evidence"],
                properties: {
                    judgement: { type: "string", enum: ["false_positive", "true_bug", "intentional", "undetermined"] },
                    summary: TEXT,
                    rationale: TEXT,
                    evidence: {
                        type: "array",
                        items: {
                            type: "object",
                            required: ["file", "line", "note"],
                            properties: { file: TEXT, line: { type: ["integer", "null"] }, note: TEXT },
                        },
                    },
                },
            },
            recommendation: { type: "string", enum: ["fix", "deviation"] },
            confidence: { type: "string", enum: ["high", "medium", "low"] },
            confidence_reason: TEXT,
            revision_drift: {
                type: "object",
                required: ["status", "detail"],
                properties: { status: { type: "string", enum: ["none", "detected", "unknown"] }, detail: TEXT },
            },
            group_excluded_cids: { type: "array", items: { type: "integer" } },
        },
    },
    fixer: {
        type: "object",
        required: ["classification", "action", "severity", "summary", "impact", "exceeded_constraints",
            "already_fixed_on_latest"],
        properties: {
            classification: TEXT, action: TEXT, severity: TEXT, summary: TEXT, impact: TEXT,
            exceeded_constraints: TEXTS, already_fixed_on_latest: { type: "boolean" },
        },
    },
    deviation: {
        type: "object",
        required: ["classification", "action", "severity", "comment"],
        properties: { classification: TEXT, action: TEXT, severity: TEXT, comment: TEXT },
    },
    reviewer: {
        type: "object",
        required: ["ok", "fix_problems", "deviation_problems"],
        properties: { ok: { type: "boolean" }, fix_problems: TEXTS, deviation_problems: TEXTS },
    },
};

// ---- 子プロセス -------------------------------------------------------------------------------

/** uv でコマンドを動かし、標準出力の JSON を返す（ok: false のときもそのまま返す）。 */
function uv(ctx, args, input = "") {
    return new Promise((resolve, reject) => {
        const child = spawn("uv", args, {
            signal: ctx.signal,
            windowsHide: true,
            env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
        });
        const out = [];
        const err = [];
        child.stdout.on("data", (chunk) => out.push(chunk));
        child.stderr.on("data", (chunk) => err.push(chunk));
        child.on("error", reject);
        child.on("close", (code) => {
            const text = Buffer.concat(out).toString("utf8");
            try {
                resolve(JSON.parse(text));
            } catch {
                const detail = Buffer.concat(err).toString("utf8").trim().slice(-600) || text.slice(-600);
                reject(new Error(`uv ${args.join(" ")} が終了コード ${code} で終わりました: ${detail}`));
            }
        });
        child.stdin.end(input);
    });
}

const ct = (ctx, ...args) => uv(ctx, ["run", "--native-tls", "--quiet", CT_PY, ...args.map(String)]);

/** MCP サーバと同じ読み取りの関数を、done が true になるまで呼ぶ。 */
async function coverity(ctx, tool, args) {
    for (;;) {
        const result = ok(await uv(ctx, ["run", "--native-tls", "--frozen", "--quiet", "--directory", SERVER_DIR,
            "coverity-triage-call", tool], JSON.stringify(args)), tool);
        if (result.done) return result;
    }
}

function ok(result, what) {
    if (result.ok) return result;
    throw new Error([`${what}: ${result.error}`, ...(result.problems ?? [])].join("\n- "));
}

const message = (error) => (error instanceof Error ? error.message : String(error));
const isText = (value) => typeof value === "string" && value.trim() !== "";

function newRunOptions(args) {
    const options = isText(args.filter) ? ["--filter", args.filter] : [];
    for (const key of ["impact", "checker", "status", "classification", "action"]) {
        for (const value of args[key] ?? []) options.push(`--${key}`, value);
    }
    if (args.limit !== undefined) options.push("--limit", args.limit);
    if (args.verify !== undefined) options.push("--verify", args.verify);
    return options;
}

// ---- 1 件の流れ（図の「1 件の流れ」）-----------------------------------------------------------

/** 作業を取り出しては調べる。続けられない問題が出たら shared.stop に理由を書いて終わる。 */
async function lane(ctx, runDir, shared) {
    for (;;) {
        const next = shared.stop ? null : await ct(ctx, "next", "--run", runDir); // [W1]
        // [W2] 取れた？
        if (next === null || (next.ok && next.item === null)) return; // [W4]
        if (!next.ok) {
            shared.stop ??= `次の作業を用意できません: ${next.error}`; // [W3]
            return;
        }
        const failed = await investigate(ctx, runDir, next, shared);
        if (failed) { // [W14]
            ok(await ct(ctx, "fail", "--run", runDir, "--item", next.item, "--reason", failed), "fail");
            ctx.log(`${next.item}: 処理できませんでした — ${failed}`);
        }
    }
}

/** 1 件を 4 つの役で調べて記録する。処理できなかったときは、その理由を返す。 */
async function investigate(ctx, runDir, next, shared) {
    const { item } = next;
    try { // [W5]
        await coverity(ctx, "get_issues",
            { repo_root: next.repo_root, stream: next.stream, cids: next.cids, output_dir: next.item_dir });
    } catch (error) {
        if (ctx.signal.aborted) throw error;
        return `警告経路を取れませんでした: ${message(error)}`;
    }
    const brief = await ct(ctx, "brief", "--run", runDir, "--item", item); // [W6]
    if (!brief.ok) return `指示書を作れませんでした: ${brief.error}`;

    // label に試行の回数を入れる（同じ prompt と選択肢の呼び出しは 1 つにまとめられるため）。
    const ask = (role, label, inputs) => ctx.agent([
        `Coverity の警告の調査のうち、あなたの役の分を行います。指示ファイル ${brief.brief} を読んでください。`,
        "指示ファイルの「すること」「終わったら」の代わりに、あなたの役の定義に従います。結果ファイルは書きません。",
        ...Object.entries(inputs).map(([name, value]) => `\n## ${name}\n\n${JSON.stringify(value, null, 1)}`),
    ].join("\n"), { agent: AGENTS[role], schema: SCHEMAS[role], label: `${item}#${next.attempt}-${label}` });

    const judgement = await ask("investigator", "investigate", {}); // [W7]
    if (judgement === null) return "調査役が答えませんでした";
    const given = { "調査役の判定": judgement };
    let [fix, deviation] = await ctx.parallel([
        () => ask("fixer", "fix", given), // [W8]
        () => ask("deviation", "deviation", given), // [W9]
    ]);
    for (let redo = 0; ; redo++) {
        if (fix === null || deviation === null) return `${fix === null ? "修正役" : "逸脱役"}が答えませんでした`;
        const review = await ask("reviewer", `review${redo}`, // [W10]
            { ...given, "修正役の案": fix, "逸脱役の案": deviation });
        if (review === null) return "確認役が答えませんでした";
        if (review.ok) break;
        if (redo === MAX_REDO) {
            return `${MAX_REDO} 回直しても確認役が通しませんでした: `
                + [...review.fix_problems, ...review.deviation_problems].join(" / ");
        }
        ctx.log(`${item}: 確認役の指摘で直します（${redo + 1} 回目）`);
        [fix, deviation] = await ctx.parallel([
            () => (review.fix_problems.length === 0 ? fix // [W11]
                : ask("fixer", `fix${redo + 1}`, { ...given, "前の案": fix, "直す点": review.fix_problems })),
            () => (review.deviation_problems.length === 0 ? deviation // [W12]
                : ask("deviation", `deviation${redo + 1}`,
                    { ...given, "前の案": deviation, "直す点": review.deviation_problems })),
        ]);
    }

    writeFileSync(brief.result_file, JSON.stringify({ // [W13]
        item,
        verdict: judgement.verdict,
        recommendation: judgement.recommendation,
        confidence: judgement.confidence,
        confidence_reason: judgement.confidence_reason,
        deviation,
        fix,
        revision_drift: judgement.revision_drift,
        group_excluded_cids: judgement.group_excluded_cids,
    }, null, 1), "utf8");
    const done = await ct(ctx, "finish", "--run", runDir, "--item", item);
    if (!done.ok) return [done.error, ...(done.problems ?? [])].join(" / ");
    ctx.log(`${item}: ${done.recommendation === "fix" ? "修正" : "逸脱"}を推奨（確信度 ${done.confidence}）`);
    if (done.warnings.length) shared.warnings.push({ item, warnings: done.warnings });
    return null;
}

// ---- 全体の流れ（図の「全体の流れ」）-----------------------------------------------------------

const triageRun = defineWorkflow({
    meta: {
        name: "coverity-triage-run",
        description: "Coverity の警告を 1 件ずつ、調査役・修正役・逸脱役・確認役の AI で調べ、修正案と逸脱コメント案を作って"
            + "一覧 summary.md を作る（/coverity-run と同じ。反映はしない）。args: repo_root（新しく始めるときの対象リポジトリの"
            + "一番上の絶対パス）か run_dir（続きから再開するときの実行フォルダ）。ほかに filter、impact、checker、status、"
            + "classification、action、limit、verify（none | build | build+analyze）",
        phases: PHASES,
        argsSchema: {
            type: "object",
            anyOf: [{ required: ["repo_root"] }, { required: ["run_dir"] }],
            properties: {
                repo_root: TEXT,
                run_dir: TEXT,
                filter: TEXT,
                impact: TEXTS,
                checker: TEXTS,
                status: TEXTS,
                classification: TEXTS,
                action: TEXTS,
                limit: { type: "integer" },
                verify: { type: "string", enum: ["none", "build", "build+analyze"] },
            },
        },
    },
    run: async (ctx) => {
        const args = ctx.args ?? {};
        ctx.phase("準備");
        if (!isText(args.repo_root) && !isText(args.run_dir)) { // [S1]
            throw new Error("repo_root（新しく始めるとき）か run_dir（続きから再開するとき）を指定してください");
        }
        // 実行フォルダは記録しておき、Copilot CLI の再開のときは同じものを使う。
        const start = await ctx.step("start-v1", async () => (isText(args.run_dir) // [S2]
            ? ok(await ct(ctx, "resume", "--run", args.run_dir), "前回の実行を開けません") // [S4]
            : ok(await ct(ctx, "new-run", "--repo", args.repo_root, ...newRunOptions(args)), "実行を作れません"))); // [S3]
        const runDir = start.run_dir;
        ctx.log(`実行フォルダ: ${runDir}`);
        const opened = ok(await ct(ctx, "resume", "--run", runDir), "止まっていた作業を戻せません"); // [S5]

        let { parallel } = opened;
        let found = null;
        if (!opened.planned) { // [S6]
            ctx.phase("検索");
            const search = await coverity(ctx, "search_issues", // [S7]
                { repo_root: opened.repo_root, filter_file: opened.filter_file, output_file: opened.issues_file });
            found = search.found;
            ctx.log(`警告 ${found} 件${search.limited ? `（上限の ${search.limit} 件で止めました）` : ""}`);
            if (found === 0) { // [S8]
                return { run_dir: runDir, summary: null, found }; // [E0]
            }
            ctx.phase("計画");
            const plan = ok(await ct(ctx, "plan", "--run", runDir), "作業の一覧を作れません"); // [S9]
            parallel = plan.parallel;
            ctx.log(`作業 ${plan.items} 件`);
        }

        ctx.phase("調査");
        const shared = { stop: null, warnings: [] };
        await ctx.parallel(Array.from({ length: Math.max(1, parallel || 1) }, () => async () => { // [S10]
            try {
                await lane(ctx, runDir, shared);
            } catch (error) {
                if (ctx.signal.aborted) throw error;
                shared.stop ??= message(error);
            }
        }));
        if (shared.stop) { // [S11]
            throw new Error(`${shared.stop}（直したら run_dir: ${runDir} で続きから再開できます）`); // [E1]
        }

        let verify = null;
        if (opened.verify !== "none") { // [S12]
            ctx.phase("検証");
            verify = ok(await ct(ctx, "verify", "--run", runDir), "ビルドで確かめられません"); // [S13]
        }
        ctx.phase("一覧");
        const summary = ok(await ct(ctx, "summary", "--run", runDir), "一覧を作れません"); // [S14]
        const status = ok(await ct(ctx, "status", "--run", runDir), "進み具合を読めません");
        return { // [E2]
            run_dir: runDir,
            summary: summary.summary,
            found,
            counts: status.counts,
            recommended: summary.recommended,
            errors: status.errors,
            warnings: shared.warnings,
            verify: verify && { build_ok: verify.build_ok ?? null, downgraded_to_low: verify.downgraded_to_low ?? [] },
            next: "summary.md の承認欄を確かめ、/coverity-apply で反映してください",
        };
    },
});

await joinSession({
    workflows: [triageRun],
    // Copilot CLI は機微な環境変数を拡張機能から外すため、Coverity の認証情報の環境変数を頼む。
    requestedEnvironmentVariables: ["COV_USER", "COV_AUTH_KEY"],
});
