// dynamic workflow coverity-triage-run（GitHub Copilot CLI の拡張機能。実験的な機能）
//
// 構造の正本は ../coverity-triage-run.md（mermaid の図と表）です。この md に合わせて書きます。
// コメントの [A1] などは、その図のノード ID です。md を直したら、このファイルも合わせて直します。
//
// ct.py install-workflow が、このフォルダを ~/.copilot/extensions/coverity-triage-run/ に写し、
// ct.py と MCP サーバの場所を書いた paths.json を隣に置きます。

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { defineWorkflow, joinSession } from "@github/copilot-sdk/extension";

const PATHS = JSON.parse(readFileSync(new URL("./paths.json", import.meta.url), "utf8"));
const WORKER = "coverity-triage-worker";
const MAX_FIX_ROUNDS = 2;
const PHASES = [
    { title: "準備", detail: "実行フォルダを作る、または途中の実行を再開する" },
    { title: "検索", detail: "Coverity で警告を探す" },
    { title: "計画", detail: "グループ化して作業の一覧を作る" },
    { title: "調査", detail: "作業ごとにサブエージェントが調べる" },
    { title: "検証", detail: "（オプション）修正案をまとめてビルドで確かめる" },
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
    return runJson(ctx, PATHS.uv, ["run", "--native-tls", "--quiet", PATHS.ct_py, ...args.map(String)], "");
}

function tool(ctx, name, args) {
    return runJson(ctx, PATHS.uv,
        ["run", "--native-tls", "--frozen", "--quiet", "--directory", PATHS.server_dir, "coverity-triage-call", name],
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

// ---- 作業の列（md の 4 章）----------------------------------------------------------------------

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

async function workItem(ctx, runDir, next, shared) {
    const item = next.item;
    const label = `${item}#${next.attempt}`;
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

    const answer = await ctx.agent(brief.prompt, { agent: WORKER, label }); // [L5]
    if (answer === null) return fail("サブエージェントが結果を返しませんでした");

    for (let round = 0; ; round++) {
        const done = await ct(ctx, "finish", "--run", runDir, "--item", item); // [L6]
        if (done.ok) {
            const split = done.split_into.length ? `。原因が違う ${done.split_into.join(", ")} を個別の作業に戻しました` : "";
            ctx.log(`${item}: ${RECOMMENDATION[done.recommendation] ?? done.recommendation}を推奨`
                + `（確信度 ${CONFIDENCE[done.confidence] ?? done.confidence}）${split}`);
            if (done.warnings.length) shared.warnings.push({ item, warnings: done.warnings });
            return;
        }
        if (!done.problems?.length || round >= MAX_FIX_ROUNDS) {
            return fail([done.error, ...(done.problems ?? [])].join(" / "));
        }
        const prompt = `${brief.prompt}\n\n結果ファイルの次の点を直してください：\n`
            + done.problems.map((p) => `- ${p}`).join("\n");
        const fixed = await ctx.agent(prompt, { agent: WORKER, label: `${label}-fix${round + 1}` }); // [L7]
        if (fixed === null) return fail("サブエージェントが直す途中で結果を返しませんでした");
    }
}

// ---- 全体の流れ（md の 3 章）--------------------------------------------------------------------

const triageRun = defineWorkflow({
    meta: {
        name: "coverity-triage-run",
        description:
            "Coverity の警告を 1 件ずつサブエージェント coverity-triage-worker に調べさせ、修正案と逸脱コメント案を作り、"
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
