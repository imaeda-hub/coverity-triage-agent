// dynamic workflow coverity-triage-run（GitHub Copilot CLI の拡張機能。実験的な機能）
//
// このプラグインの com.github.copilot/extensions/ に置くと、プラグインを入れたときに Copilot CLI が読み込む
// （Copilot CLI 1.0.79 の変更履歴「Agent Plugins spec plugins can now ship extensions under a
// com.github.copilot/extensions/ directory」）。
// 流れを図にしたものが docs/dynamic-workflow.md です。コメントの [A1] などは、その図のノード ID です。
// 図を直したら、このファイルも合わせて直します（tests/test_workflow.py がノード ID の対応を確かめます）。

import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineWorkflow, joinSession } from "@github/copilot-sdk/extension";

// このファイルは <プラグイン>/com.github.copilot/extensions/coverity-triage-run/extension.mjs にある。
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
const TEXT = { type: "string" };
const TEXTS = { type: "array", items: TEXT };
const ATTRIBUTES = { classification: TEXT, action: TEXT, severity: TEXT };
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
                            type: "object", required: ["file"],
                            properties: { file: TEXT, line: { type: ["integer", "null"] }, note: TEXT },
                        },
                    },
                },
            },
            recommendation: { type: "string", enum: ["fix", "deviation"] },
            confidence: { type: "string", enum: ["high", "medium", "low"] },
            confidence_reason: TEXT,
            revision_drift: {
                type: "object", required: ["status"],
                properties: { status: { type: "string", enum: ["none", "detected", "unknown"] }, detail: TEXT },
            },
            group_excluded_cids: { type: "array", items: { type: "integer" } },
        },
    },
    fixer: {
        type: "object",
        required: ["classification", "action", "severity", "summary", "impact", "exceeded_constraints",
            "already_fixed_on_latest"],
        properties: { ...ATTRIBUTES, summary: TEXT, impact: TEXT, exceeded_constraints: TEXTS,
            already_fixed_on_latest: { type: "boolean" } },
    },
    deviation: {
        type: "object",
        required: ["classification", "action", "severity", "comment"],
        properties: { ...ATTRIBUTES, comment: TEXT },
    },
    reviewer: {
        type: "object",
        required: ["ok", "fix_problems", "deviation_problems"],
        properties: { ok: { type: "boolean" }, fix_problems: TEXTS, deviation_problems: TEXTS },
    },
};
const PHASES = [
    { title: "準備", detail: "新しい実行を用意する、または前回の実行を開く" },
    { title: "検索", detail: "Coverity で警告を探す" },
    { title: "計画", detail: "似た警告をまとめて作業の一覧を作る" },
    { title: "調査", detail: "作業を 1 件ずつ調べる" },
    { title: "検証", detail: "修正案をまとめてビルドで確かめる（設定したときだけ）" },
    { title: "一覧", detail: "一覧 summary.md を作る" },
];
const LIST = { type: "array", items: { type: "string" } };
const RECOMMENDATION = { fix: "修正", deviation: "逸脱" };
const CONFIDENCE = { high: "高", medium: "中", low: "低" };

// ---- 子プロセス（ct.py と coverity-triage-call）-----------------------------------------------

/** コマンドを動かし、標準出力の JSON を 1 つ返す（ok: false でも返す）。 */
function runJson(ctx, command, args, input) {
    return new Promise((resolve, reject) => {
        const child = spawn(command, args, {
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
            const text = Buffer.concat(out).toString("utf8").trim();
            try {
                resolve(JSON.parse(text));
            } catch {
                const tail = Buffer.concat(err).toString("utf8").trim().slice(-800);
                reject(new Error(`${args.join(" ")} の結果を読めません（終了コード ${code}）: ${tail || text.slice(-800)}`));
            }
        });
        child.stdin.end(input);
    });
}

function ct(ctx, ...args) {
    return runJson(ctx, "uv", ["run", "--native-tls", "--quiet", CT_PY, ...args.map(String)], "");
}

function tool(ctx, name, args) {
    return runJson(ctx, "uv",
        ["run", "--native-tls", "--frozen", "--quiet", "--directory", SERVER_DIR, "coverity-triage-call", name],
        JSON.stringify(args));
}

/** done が true になるまで、同じ引数でツールを呼ぶ。 */
async function untilDone(ctx, name, args) {
    for (;;) {
        const result = must(await tool(ctx, name, args), name);
        if (result.done) return result;
    }
}

function must(result, what) {
    if (result.ok) return result;
    const problems = (result.problems ?? []).map((p) => `\n- ${p}`).join("");
    throw new Error(`${what}: ${result.error}${problems}`);
}

function message(error) {
    return error instanceof Error ? error.message : String(error);
}

function isText(value) {
    return typeof value === "string" && value.trim() !== "";
}

function newRunOptions(args) {
    const options = [];
    if (isText(args.filter)) options.push("--filter", args.filter);
    for (const [key, flag] of [["impact", "--impact"], ["checker", "--checker"], ["status", "--status"],
        ["classification", "--classification"], ["action", "--action"]]) {
        for (const value of args[key] ?? []) options.push(flag, value);
    }
    if (args.limit !== undefined) options.push("--limit", args.limit);
    if (args.verify !== undefined) options.push("--verify", args.verify);
    return options;
}

// ---- 1 件の流れ----------------------------------------------------------------------

async function lane(ctx, runDir, shared) {
    try {
        while (!shared.stop) {
            const next = await ct(ctx, "next", "--run", runDir); // [L1]
            if (!next.ok) { // [L2]
                shared.stop ??= `次の作業を用意できません: ${next.error}`; // [L9]
                return;
            }
            if (next.item === null) return; // [L10]
            await workItem(ctx, runDir, next, shared);
        }
    } catch (error) {
        if (ctx.signal.aborted) throw error;
        shared.stop ??= message(error); // [L9]
    }
}

/** 1 つの役の AI を呼ぶ。返事は schema の形の JSON（失敗は null）。 */
function askRole(ctx, role, brief, label, inputs) {
    const prompt = [
        `Coverity の警告を調べる作業の、あなたの役の分を行います。指示ファイル ${brief.brief} を読んでください。`,
        "指示ファイルの「すること」と「終わったら」の代わりに、あなたの役の定義に従います。結果ファイルは書きません。",
        ...Object.entries(inputs).map(([name, value]) => `\n## ${name}\n\n${JSON.stringify(value, null, 1)}`),
    ].join("\n");
    return ctx.agent(prompt, { agent: AGENTS[role], schema: SCHEMAS[role], label });
}

async function workItem(ctx, runDir, next, shared) {
    const item = next.item;
    const base = `${item}#${next.attempt}`;
    const fail = async (reason) => { // [L8]
        must(await ct(ctx, "fail", "--run", runDir, "--item", item, "--reason", reason), `${item} を記録できません`);
        ctx.log(`${item}: 処理できませんでした — ${reason}`);
    };

    try { // [L3]
        await untilDone(ctx, "get_issues",
            { repo_root: next.repo_root, stream: next.stream, cids: next.cids, output_dir: next.item_dir });
    } catch (error) {
        if (ctx.signal.aborted) throw error;
        return fail(`警告経路を取れませんでした: ${message(error)}`);
    }
    const brief = await ct(ctx, "brief", "--run", runDir, "--item", item); // [L4]
    if (!brief.ok) return fail(`指示ファイルを作れませんでした: ${brief.error}`);

    const investigation = await askRole(ctx, "investigator", brief, `${base}-investigate`, {}); // [L5]
    if (investigation === null) return fail("調査役が答えませんでした");

    const judged = { "調査役の判定": investigation };
    let [fix, deviation] = await ctx.parallel([
        () => askRole(ctx, "fixer", brief, `${base}-fix`, judged), // [L12]
        () => askRole(ctx, "deviation", brief, `${base}-deviation`, judged), // [L13]
    ]);
    for (let redo = 0; ; redo++) {
        if (fix === null || deviation === null) return fail(`${fix === null ? "修正役" : "逸脱役"}が答えませんでした`);
        const review = await askRole(ctx, "reviewer", brief, `${base}-review${redo || ""}`, // [L14]
            { ...judged, "修正役の結果": fix, "逸脱役の結果": deviation });
        if (review === null) return fail("確認役が答えませんでした");
        if (review.ok) break;
        if (redo >= MAX_REDO) {
            return fail(`${MAX_REDO} 回直しても確認役が通しませんでした: `
                + [...review.fix_problems, ...review.deviation_problems].join(" / "));
        }
        ctx.log(`${item}: 確認役の指摘で直します（${redo + 1} 回目）`);
        [fix, deviation] = await ctx.parallel([
            () => (review.fix_problems.length ? askRole(ctx, "fixer", brief, `${base}-fix${redo + 1}`, // [L16]
                { ...judged, "前の結果": fix, "直す点": review.fix_problems }) : fix),
            () => (review.deviation_problems.length ? askRole(ctx, "deviation", brief, // [L17]
                `${base}-deviation${redo + 1}`, { ...judged, "前の結果": deviation, "直す点": review.deviation_problems })
                : deviation),
        ]);
    }

    writeFileSync(brief.result_file, JSON.stringify({ // [L6]
        item,
        verdict: investigation.verdict,
        recommendation: investigation.recommendation,
        confidence: investigation.confidence,
        confidence_reason: investigation.confidence_reason,
        deviation,
        fix,
        revision_drift: investigation.revision_drift,
        group_excluded_cids: investigation.group_excluded_cids,
    }, null, 1), "utf8");
    const done = await ct(ctx, "finish", "--run", runDir, "--item", item);
    if (!done.ok) return fail([done.error, ...(done.problems ?? [])].join(" / "));
    const split = done.split_into.length ? `。原因が違う ${done.split_into.join(", ")} を個別の作業に戻しました` : "";
    ctx.log(`${item}: ${RECOMMENDATION[done.recommendation] ?? done.recommendation}を推奨`
        + `（確信度 ${CONFIDENCE[done.confidence] ?? done.confidence}）${split}`);
    if (done.warnings.length) shared.warnings.push({ item, warnings: done.warnings });
}

// ---- 全体の流れ--------------------------------------------------------------------

const triageRun = defineWorkflow({
    meta: {
        name: "coverity-triage-run",
        description:
            "Coverity の警告を 1 件ずつ、調査役・修正役・逸脱役・確認役の AI に調べさせ、修正案と逸脱コメント案を作り、"
            + "一覧 summary.md を作る（/coverity-run と同じ処理。反映はしない）。"
            + "args: { repo_root?: string（新しく始めるときの対象リポジトリの一番上の絶対パス）, "
            + "run_dir?: string（途中の実行を再開するときの実行フォルダ）, filter?: string, impact?: string[], "
            + "checker?: string[], status?: string[], classification?: string[], action?: string[], "
            + "limit?: integer, verify?: \"none\" | \"build\" | \"build+analyze\" } — repo_root か run_dir のどちらかが要る。",
        phases: PHASES,
        argsSchema: {
            type: "object",
            anyOf: [{ required: ["repo_root"] }, { required: ["run_dir"] }],
            properties: {
                repo_root: { type: "string" },
                run_dir: { type: "string" },
                filter: { type: "string" },
                impact: LIST,
                checker: LIST,
                status: LIST,
                classification: LIST,
                action: LIST,
                limit: { type: "integer" },
                verify: { type: "string", enum: ["none", "build", "build+analyze"] },
            },
        },
    },
    run: async (ctx) => {
        const args = ctx.args ?? {};
        if (!isText(args.run_dir) && !isText(args.repo_root)) { // [A1]
            throw new Error("repo_root（新しく始めるとき）か run_dir（再開するとき）を絶対パスで指定してください");
        }

        ctx.phase("準備");
        const start = await ctx.step("start-v1", async () => (isText(args.run_dir) // [A2]
            ? must(await ct(ctx, "resume", "--run", args.run_dir), "途中の実行を開けません") // [A4]
            : must(await ct(ctx, "new-run", "--repo", args.repo_root, ...newRunOptions(args)), "実行を始められません") // [A3]
        ));
        const runDir = start.run_dir;
        ctx.log(`実行フォルダ: ${runDir}`);
        if (start.note) ctx.log(start.note);
        const opened = must(await ct(ctx, "resume", "--run", runDir), "途中の作業を戻せません"); // [A5]

        let found = null;
        let parallel = opened.parallel;
        if (!opened.planned) { // [A6]
            ctx.phase("検索");
            const search = await untilDone(ctx, "search_issues", // [B1]
                { repo_root: opened.repo_root, filter_file: opened.filter_file, output_file: opened.issues_file });
            found = search.found;
            ctx.log(`警告 ${found} 件` + (search.limited ? `（上限の ${search.limit} 件で止めました）` : ""));
            if (found === 0) { // [B2]
                return { // [Z1]
                    run_dir: runDir, summary: null, found, counts: opened.counts, recommended: null, errors: [],
                    warnings: [], verify: null, next: "条件に合う警告はありませんでした",
                };
            }
            ctx.phase("計画");
            const plan = must(await ct(ctx, "plan", "--run", runDir), "作業の一覧を作れません"); // [B3]
            parallel = plan.parallel;
            ctx.log(`作業 ${plan.items} 件（グループ ${plan.groups.length} 件）`);
            for (const note of plan.notes) ctx.log(note);
        }

        ctx.phase("調査");
        const shared = { stop: null, warnings: [] };
        const width = Number.isInteger(parallel) && parallel > 0 ? parallel : 1;
        await ctx.parallel(Array.from({ length: width }, () => () => lane(ctx, runDir, shared))); // [C1]
        if (shared.stop) { // [C2]
            throw new Error(`${shared.stop}（実行フォルダ ${runDir}。直したら run_dir を渡して続きから再開できます）`); // [Z2]
        }

        let verify = null;
        if (opened.verify !== "none") { // [D1]
            ctx.phase("検証");
            verify = must(await ct(ctx, "verify", "--run", runDir), "ビルドで確かめられません"); // [D2]
            ctx.log(`ビルド: ${verify.build_ok ? "できました" : "できませんでした"}`
                + (verify.downgraded_to_low?.length ? `。確信度を「低」に下げた作業: ${verify.downgraded_to_low.join(", ")}` : ""));
        }

        ctx.phase("一覧");
        const summary = must(await ct(ctx, "summary", "--run", runDir), "一覧を作れません"); // [E1]
        const status = must(await ct(ctx, "status", "--run", runDir), "進み具合を読めません");
        return { // [Z3]
            run_dir: runDir,
            summary: summary.summary,
            found,
            counts: status.counts,
            recommended: summary.recommended,
            errors: status.errors,
            warnings: shared.warnings,
            verify: verify && { mode: verify.mode, build_ok: verify.build_ok ?? null,
                downgraded_to_low: verify.downgraded_to_low ?? [], log: verify.log ?? null },
            next: "summary.md の承認欄を確かめてください。AI の推奨を入れてあるので、変えたい行だけ「修正」「逸脱」「却下」に"
                + "書き換えます。逸脱コメントは各詳細レポートの「案A: 逸脱」で直せます。終わったら /coverity-apply で反映します",
        };
    },
});

await joinSession({
    workflows: [triageRun],
    // Copilot CLI は機微な環境変数を拡張機能から外すため、Coverity の認証情報（既定の名前）を頼む。
    requestedEnvironmentVariables: ["COV_USER", "COV_AUTH_KEY"],
});
