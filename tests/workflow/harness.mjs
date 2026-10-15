// Loads extension.mjs with the SDK test double, then prints its registration ("meta") or runs the workflow
// ("run") with a context made after the SDK's docs/workflows.md. The four role agents are played here: each
// returns its part of config.result, and the fixer also edits the fix copy. Usage: node harness.mjs config.json
import { appendFileSync, readFileSync } from "node:fs";
import { register } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

register("./hooks.mjs", import.meta.url);
const config = JSON.parse(readFileSync(process.argv[2], "utf8"));
const sdk = await import("@github/copilot-sdk/extension");
await import(pathToFileURL(config.extension).href);
const [session] = sdk.sessions;

if (config.mode === "meta") {
    process.stdout.write(JSON.stringify({
        metas: session.workflows.map((w) => w.meta),
        requestedEnvironmentVariables: session.requestedEnvironmentVariables,
    }));
    process.exit(0);
}

const calls = [];
const logs = [];
const phases = [];
const steps = new Map();
const sentBack = new Set();

function play(prompt, { agent, label }) {
    const brief = readFileSync(/指示ファイル (\S+) を読んで/.exec(prompt)[1], "utf8");
    const item = /^# 調査の指示: ([^\r\n]+)/m.exec(brief)[1];
    const { result } = config;
    if (agent === "coverity-triage-investigator") {
        const { verdict, recommendation, confidence, confidence_reason, revision_drift } = result;
        return { verdict, recommendation, confidence, confidence_reason, revision_drift, group_excluded_cids: [] };
    }
    if (agent === "coverity-triage-fixer") {
        if (label.endsWith("-fix")) {
            const fixDir = /修正するコード（ここだけを変更する） \| `([^`]+)`/.exec(brief)[1];
            appendFileSync(join(fixDir, "src", "reader.c"), `/* fix ${item} */\n`);
        }
        return result.fix;
    }
    if (agent === "coverity-triage-deviation") return result.deviation;
    const sendBack = (config.send_back ?? []).includes(item) && !sentBack.has(item);
    sentBack.add(item);
    return { ok: !sendBack, fix_problems: sendBack ? ["fclose の後で fp を NULL にする"] : [], deviation_problems: [] };
}

const ctx = {
    runId: "test-run",
    args: config.args ?? {},
    signal: new AbortController().signal,
    session: {},
    phase: (title) => phases.push(title),
    log: (line) => logs.push(line),
    async step(key, producer) {
        if (!steps.has(key)) steps.set(key, JSON.parse(JSON.stringify(await producer())));
        return steps.get(key);
    },
    // A thunk that throws becomes null (docs/workflows.md).
    parallel: (thunks) => Promise.all(thunks.map((thunk) => Promise.resolve().then(thunk).catch(() => null))),
    async agent(prompt, options = {}) {
        calls.push({ agent: options.agent, label: options.label });
        const answer = play(prompt, options);
        for (const key of options.schema?.required ?? []) {
            if (!(key in answer)) throw new Error(`${options.agent}: ${key} がありません`);
        }
        return answer;
    },
};

let outcome;
try {
    outcome = { ok: true, result: await session.workflows[0].run(ctx) };
} catch (error) {
    outcome = { ok: false, error: error.message };
}
process.stdout.write(JSON.stringify({ ...outcome, calls, logs, phases }));
