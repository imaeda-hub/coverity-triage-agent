// dynamic workflow coverity-triage-run（GitHub Copilot CLI の拡張機能。実験的な機能）
//
// 処理の流れは、下の 2 つのグラフ RUN（全体）と ITEM（1 件）に、ノードと矢印のデータとして書きます。
// ../coverity-triage-run.md の mermaid の図は、このグラフをそのまま絵にしたものです（ノード ID と矢印のラベルが同じ）。
// 図を直したら、このグラフを合わせて直します（tests/test_workflow.py が一致を確かめます）。
//
// ct.py install-workflow が、このフォルダを ~/.copilot/extensions/coverity-triage-run/ に写し、
// ct.py と MCP サーバの場所を書いた paths.json を隣に置きます。

import { spawn } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { defineWorkflow, joinSession } from "@github/copilot-sdk/extension";

const PATHS = JSON.parse(readFileSync(new URL("./paths.json", import.meta.url), "utf8"));
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

// ---- グラフの実行 ----------------------------------------------------------------------------
//
// ノードの run(ctx, state) は、次に進む矢印のラベルを返す（ラベルの無い矢印なら何も返さない。複数なら配列）。
// 選ばれた矢印が 2 本以上なら、その先を ctx.parallel で同時に動かし、join: true のノードで待ち合わせる。
// end: true のノードで終わる。

function graph(start, nodes, edges) {
    return { start, nodes, edges: edges.map(([from, label, to]) => ({ from, label, to })) };
}

async function runGraph(ctx, g, state, from = g.start, stopAtJoin = false) {
    for (let node = from; ;) {
        const spec = g.nodes[node];
        if (stopAtJoin && node !== from && spec.join) return node;
        const outcome = await spec.run(ctx, state);
        if (spec.end) return node;
        const labels = outcome === undefined ? [null] : [outcome].flat();
        const targets = labels.flatMap((label) => g.edges.filter((e) => e.from === node && e.label === label)
            .map((e) => e.to));
        if (targets.length === 0) throw new Error(`グラフに ${node} から「${labels.join("、")}」への矢印がありません`);
        if (targets.length === 1) {
            node = targets[0];
            continue;
        }
        const errors = [];
        const joins = await ctx.parallel(targets.map((to) => async () => {
            try {
                return await runGraph(ctx, g, state, to, true);
            } catch (error) {
                errors.push(error);
                throw error;
            }
        }));
        if (errors.length) throw errors[0];
        if (new Set(joins).size !== 1) throw new Error(`グラフの ${targets.join("、")} が 1 つのノードで合流しません`);
        node = joins[0];
    }
}

// ---- AI の呼び出し ----------------------------------------------------------------------------

/** 1 つの役の AI を呼ぶ。返事は schema の形の JSON（失敗は null）。 */
function askRole(ctx, role, s, label, inputs) {
    const prompt = [
        `Coverity の警告を調べる作業の、あなたの役の分を行います。指示ファイル ${s.brief.brief} を読んでください。`,
        "指示ファイルの「すること」と「終わったら」の代わりに、あなたの役の定義に従います。結果ファイルは書きません。",
        ...Object.entries(inputs).map(([name, value]) => `\n## ${name}\n\n${JSON.stringify(value, null, 1)}`),
    ].join("\n");
    return ctx.agent(prompt, { agent: AGENTS[role], schema: SCHEMAS[role], label: `${s.base}-${label}` });
}

const judged = (s) => ({ "調査役の判定": s.investigation });

// ---- 1 件のグラフ（列ごとに動かす。state は列ごと）----------------------------------------

export const ITEM = graph("L1", {
    L1: { // 次の作業を取る
        run: async (ctx, s) => {
            s.next = s.shared.stop ? null : await ct(ctx, "next", "--run", s.runDir);
        },
    },
    L2: { // 取れた？
        run: (ctx, s) => {
            if (s.next === null) return "ほかの列が止めた";
            if (!s.next.ok) return "問題があって取れない";
            if (s.next.item === null) return "残りがない";
            Object.assign(s, { item: s.next.item, base: `${s.next.item}#${s.next.attempt}`, reason: null,
                brief: null, investigation: null, fix: null, deviation: null, review: null, redo: 0 });
            return "取れた";
        },
    },
    L3: { // Coverity から警告の経路を取る
        run: async (ctx, s) => {
            try {
                await untilDone(ctx, "get_issues", { repo_root: s.next.repo_root, stream: s.next.stream,
                    cids: s.next.cids, output_dir: s.next.item_dir });
            } catch (error) {
                if (ctx.signal.aborted) throw error;
                s.reason = `警告経路を取れませんでした: ${message(error)}`;
                return "失敗";
            }
        },
    },
    L4: { // 指示書を作る
        run: async (ctx, s) => {
            s.brief = await ct(ctx, "brief", "--run", s.runDir, "--item", s.item);
            if (s.brief.ok) return undefined;
            s.reason = `指示ファイルを作れませんでした: ${s.brief.error}`;
            return "失敗";
        },
    },
    L5: { // 調査役
        agent: "investigator",
        run: async (ctx, s) => {
            s.investigation = await askRole(ctx, "investigator", s, "investigate", {});
            if (s.investigation !== null) return undefined;
            s.reason = "調査役が答えませんでした";
            return "答えない";
        },
    },
    L12: { // 修正役
        agent: "fixer",
        run: async (ctx, s) => {
            s.fix = await askRole(ctx, "fixer", s, "fix", judged(s));
        },
    },
    L13: { // 逸脱役
        agent: "deviation",
        run: async (ctx, s) => {
            s.deviation = await askRole(ctx, "deviation", s, "deviation", judged(s));
        },
    },
    L14: { // 確認役
        agent: "reviewer",
        join: true,
        run: async (ctx, s) => {
            if (s.fix === null || s.deviation === null) {
                s.reason = `${s.fix === null ? "修正役" : "逸脱役"}が答えませんでした`;
                return "案がない";
            }
            const review = await askRole(ctx, "reviewer", s, `review${s.redo || ""}`,
                { ...judged(s), "修正役の結果": s.fix, "逸脱役の結果": s.deviation });
            if (review === null) {
                s.reason = "確認役が答えませんでした";
                return "答えない";
            }
            if (review.ok || (!review.fix_problems.length && !review.deviation_problems.length)) return "よい";
            if (s.redo >= MAX_REDO) {
                s.reason = `${MAX_REDO} 回直しても確認役が通しませんでした: `
                    + [...review.fix_problems, ...review.deviation_problems].join(" / ");
                return "2 回直してもだめ";
            }
            s.review = review;
            s.redo += 1;
            ctx.log(`${s.item}: 確認役の指摘で直します（${s.redo} 回目）`);
            return [...(review.fix_problems.length ? ["修正案に直す点"] : []),
                ...(review.deviation_problems.length ? ["逸脱案に直す点"] : [])];
        },
    },
    L16: { // 修正役に直させる
        agent: "fixer",
        run: async (ctx, s) => {
            s.fix = await askRole(ctx, "fixer", s, `fix${s.redo}`,
                { ...judged(s), "前の結果": s.fix, "直す点": s.review.fix_problems });
        },
    },
    L17: { // 逸脱役に直させる
        agent: "deviation",
        run: async (ctx, s) => {
            s.deviation = await askRole(ctx, "deviation", s, `deviation${s.redo}`,
                { ...judged(s), "前の結果": s.deviation, "直す点": s.review.deviation_problems });
        },
    },
    L6: { // 結果をまとめて記録する
        run: async (ctx, s) => {
            const { investigation: inv } = s;
            writeFileSync(s.brief.result_file, JSON.stringify({
                item: s.item, verdict: inv.verdict, recommendation: inv.recommendation, confidence: inv.confidence,
                confidence_reason: inv.confidence_reason, deviation: s.deviation, fix: s.fix,
                revision_drift: inv.revision_drift, group_excluded_cids: inv.group_excluded_cids,
            }, null, 1), "utf8");
            const done = await ct(ctx, "finish", "--run", s.runDir, "--item", s.item);
            if (!done.ok) {
                s.reason = [done.error, ...(done.problems ?? [])].join(" / ");
                return "記録できない";
            }
            const split = done.split_into.length ? `。原因が違う ${done.split_into.join(", ")} を個別の作業に戻しました` : "";
            ctx.log(`${s.item}: ${RECOMMENDATION[done.recommendation] ?? done.recommendation}を推奨`
                + `（確信度 ${CONFIDENCE[done.confidence] ?? done.confidence}）${split}`);
            if (done.warnings.length) s.shared.warnings.push({ item: s.item, warnings: done.warnings });
        },
    },
    L8: { // 「処理できなかった」と記録
        run: async (ctx, s) => {
            must(await ct(ctx, "fail", "--run", s.runDir, "--item", s.item, "--reason", s.reason),
                `${s.item} を記録できません`);
            ctx.log(`${s.item}: 処理できませんでした — ${s.reason}`);
        },
    },
    L9: { // 全体を止める
        end: true,
        run: (ctx, s) => {
            s.shared.stop ??= `次の作業を用意できません: ${s.next.error}`;
        },
    },
    L10: { end: true, run: () => {} }, // 終わり
}, [
    ["L1", null, "L2"],
    ["L2", "問題があって取れない", "L9"],
    ["L2", "残りがない", "L10"],
    ["L2", "ほかの列が止めた", "L10"],
    ["L2", "取れた", "L3"],
    ["L3", "失敗", "L8"],
    ["L3", null, "L4"],
    ["L4", "失敗", "L8"],
    ["L4", null, "L5"],
    ["L5", "答えない", "L8"],
    ["L5", null, "L12"],
    ["L5", null, "L13"],
    ["L12", null, "L14"],
    ["L13", null, "L14"],
    ["L14", "よい", "L6"],
    ["L14", "修正案に直す点", "L16"],
    ["L14", "逸脱案に直す点", "L17"],
    ["L14", "2 回直してもだめ", "L8"],
    ["L14", "答えない", "L8"],
    ["L14", "案がない", "L8"],
    ["L16", null, "L14"],
    ["L17", null, "L14"],
    ["L6", "記録できない", "L8"],
    ["L6", null, "L1"],
    ["L8", null, "L1"],
]);

// ---- 全体のグラフ ------------------------------------------------------------------------------

export const RUN = graph("A1", {
    A1: { // 始める
        run: (ctx, s) => {
            if (!isText(s.args.run_dir) && !isText(s.args.repo_root)) {
                throw new Error("repo_root（新しく始めるとき）か run_dir（再開するとき）を絶対パスで指定してください");
            }
            ctx.phase("準備");
        },
    },
    A2: { run: (ctx, s) => (isText(s.args.run_dir) ? "はい" : "いいえ") }, // 前回の続き？
    A3: { // 新しい実行を用意する
        run: async (ctx, s) => {
            s.start = await ctx.step("start-v1", async () => must(
                await ct(ctx, "new-run", "--repo", s.args.repo_root, ...newRunOptions(s.args)), "実行を始められません"));
        },
    },
    A4: { // 前回の実行を開く
        run: async (ctx, s) => {
            s.start = await ctx.step("start-v1", async () => must(
                await ct(ctx, "resume", "--run", s.args.run_dir), "途中の実行を開けません"));
        },
    },
    A5: { // 途中で止まっていた作業をやり直しに戻す
        run: async (ctx, s) => {
            s.runDir = s.start.run_dir;
            ctx.log(`実行フォルダ: ${s.runDir}`);
            if (s.start.note) ctx.log(s.start.note);
            s.opened = must(await ct(ctx, "resume", "--run", s.runDir), "途中の作業を戻せません");
            s.parallel = s.opened.parallel;
        },
    },
    A6: { run: (ctx, s) => (s.opened.planned ? "ある" : "ない") }, // 作業の一覧はもうある？
    B1: { // Coverity で警告を探す
        run: async (ctx, s) => {
            ctx.phase("検索");
            const { opened } = s;
            const search = await untilDone(ctx, "search_issues",
                { repo_root: opened.repo_root, filter_file: opened.filter_file, output_file: opened.issues_file });
            s.found = search.found;
            ctx.log(`警告 ${s.found} 件` + (search.limited ? `（上限の ${search.limit} 件で止めました）` : ""));
        },
    },
    B2: { run: (ctx, s) => (s.found === 0 ? "0 件" : "1 件以上") }, // 見つかった？
    Z1: { // 終わり（調べるものなし）
        end: true,
        run: (ctx, s) => {
            s.result = { run_dir: s.runDir, summary: null, found: 0, counts: s.opened.counts, recommended: null,
                errors: [], warnings: [], verify: null, next: "条件に合う警告はありませんでした" };
        },
    },
    B3: { // 似た警告をまとめて作業の一覧を作る
        run: async (ctx, s) => {
            ctx.phase("計画");
            const plan = must(await ct(ctx, "plan", "--run", s.runDir), "作業の一覧を作れません");
            s.parallel = plan.parallel;
            ctx.log(`作業 ${plan.items} 件（グループ ${plan.groups.length} 件）`);
            for (const note of plan.notes) ctx.log(note);
        },
    },
    C1: { // 作業を 1 件ずつ調べる（1 件のグラフを parallel 本、同時に）
        run: async (ctx, s) => {
            ctx.phase("調査");
            const width = Number.isInteger(s.parallel) && s.parallel > 0 ? s.parallel : 1;
            await ctx.parallel(Array.from({ length: width }, () => async () => {
                try {
                    await runGraph(ctx, ITEM, { runDir: s.runDir, shared: s.shared });
                } catch (error) {
                    if (ctx.signal.aborted) throw error;
                    s.shared.stop ??= message(error);
                }
            }));
        },
    },
    C2: { run: (ctx, s) => (s.shared.stop ? "出た" : "出ない") }, // 続けられない問題が出た？
    Z2: { // 止めて知らせる
        end: true,
        run: (ctx, s) => {
            throw new Error(`${s.shared.stop}（実行フォルダ ${s.runDir}。直したら run_dir を渡して続きから再開できます）`);
        },
    },
    D1: { run: (ctx, s) => (s.opened.verify !== "none" ? "はい" : "いいえ") }, // ビルドで確かめる設定？
    D2: { // 修正案をまとめてビルドで確かめる
        run: async (ctx, s) => {
            ctx.phase("検証");
            s.verify = must(await ct(ctx, "verify", "--run", s.runDir), "ビルドで確かめられません");
            ctx.log(`ビルド: ${s.verify.build_ok ? "できました" : "できませんでした"}`
                + (s.verify.downgraded_to_low?.length ? `。確信度を「低」に下げた作業: ${s.verify.downgraded_to_low.join(", ")}` : ""));
        },
    },
    E1: { // 一覧 summary.md を作る
        run: async (ctx, s) => {
            ctx.phase("一覧");
            s.summary = must(await ct(ctx, "summary", "--run", s.runDir), "一覧を作れません");
            s.status = must(await ct(ctx, "status", "--run", s.runDir), "進み具合を読めません");
        },
    },
    Z3: { // 終わり
        end: true,
        run: (ctx, s) => {
            const { verify } = s;
            s.result = {
                run_dir: s.runDir, summary: s.summary.summary, found: s.found, counts: s.status.counts,
                recommended: s.summary.recommended, errors: s.status.errors, warnings: s.shared.warnings,
                verify: verify && { mode: verify.mode, build_ok: verify.build_ok ?? null,
                    downgraded_to_low: verify.downgraded_to_low ?? [], log: verify.log ?? null },
                next: "summary.md の承認欄を確かめてください。AI の推奨を入れてあるので、変えたい行だけ「修正」「逸脱」「却下」に"
                    + "書き換えます。逸脱コメントは各詳細レポートの「案A: 逸脱」で直せます。終わったら /coverity-apply で反映します",
            };
        },
    },
}, [
    ["A1", null, "A2"],
    ["A2", "いいえ", "A3"],
    ["A2", "はい", "A4"],
    ["A3", null, "A5"],
    ["A4", null, "A5"],
    ["A5", null, "A6"],
    ["A6", "ない", "B1"],
    ["A6", "ある", "C1"],
    ["B1", null, "B2"],
    ["B2", "0 件", "Z1"],
    ["B2", "1 件以上", "B3"],
    ["B3", null, "C1"],
    ["C1", null, "C2"],
    ["C2", "出た", "Z2"],
    ["C2", "出ない", "D1"],
    ["D1", "はい", "D2"],
    ["D1", "いいえ", "E1"],
    ["D2", null, "E1"],
    ["E1", null, "Z3"],
]);

// ---- 登録 --------------------------------------------------------------------------------------

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
        const state = { args: ctx.args ?? {}, found: null, verify: null, shared: { stop: null, warnings: [] } };
        await runGraph(ctx, RUN, state);
        return state.result;
    },
});

await joinSession({
    workflows: [triageRun],
    // Copilot CLI は機微な環境変数を拡張機能から外すため、Coverity の認証情報（既定の名前）を頼む。
    requestedEnvironmentVariables: ["COV_USER", "COV_AUTH_KEY"],
});
