// Loads the installed extension with the SDK stub and either prints its registration ("meta") or runs
// the workflow ("run") with a context that behaves like the documented WorkflowContext. The role agents
// are played by returning what they would return (and, for the fixer, an edit in the fix copy).
//
// Usage: node harness.mjs <config.json>
//   config: { extension, mode, args?, result?, bad_first?: [item ids] }
import { appendFileSync, readFileSync } from "node:fs";
import { register } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

register("./sdk-hooks.mjs", import.meta.url);
const config = JSON.parse(readFileSync(process.argv[2], "utf8"));
const sdk = await import("@github/copilot-sdk/extension");
await import(pathToFileURL(config.extension).href);
const [joined] = sdk.joined;

if (config.mode === "meta") {
    process.stdout.write(JSON.stringify({
        workflows: joined.workflows.map((w) => w.meta),
        requestedEnvironmentVariables: joined.requestedEnvironmentVariables,
    }));
    process.exit(0);
}

const agents = [];
const phases = [];
const logs = [];
const journal = new Map();
const badFirst = new Set(config.bad_first ?? []);

// Plays the four role agents. config.result is a whole result.json; each role returns its part.
// bad_first: items whose first fix the reviewer sends back once.
const reviewed = new Set();

function worker(prompt, options) {
    const brief = readFileSync(/指示ファイル (.+?) を読んで/.exec(prompt)[1], "utf8");
    const item = /^# 調査の指示: ([^\r\n]+)/m.exec(brief)[1];
    const fixDir = /\| 修正するコード（ここだけを変更する） \| `(.+?)` \|/.exec(brief)[1];
    const { result } = config;
    switch (options.agent) {
    case "coverity-triage-investigator":
        return { verdict: result.verdict, recommendation: result.recommendation, confidence: result.confidence,
            confidence_reason: result.confidence_reason, revision_drift: result.revision_drift, group_excluded_cids: [] };
    case "coverity-triage-fixer":
        if (!/-fix\d/.test(options.label)) appendFileSync(join(fixDir, "src", "reader.c"), `/* fix ${item} */\n`);
        return { exceeded_constraints: [], already_fixed_on_latest: false, ...result.fix };
    case "coverity-triage-deviation":
        return result.deviation;
    case "coverity-triage-reviewer": {
        const sendBack = badFirst.has(item) && !reviewed.has(item);
        reviewed.add(item);
        return { ok: !sendBack, fix_problems: sendBack ? ["fclose の後で fp を NULL にする"] : [], deviation_problems: [] };
    }
    default:
        throw new Error(`unknown agent ${options.agent}`);
    }
}

const ctx = {
    runId: "test-run",
    args: config.args ?? {},
    signal: new AbortController().signal,
    session: {},
    phase: (title) => phases.push(title),
    log: (line) => logs.push(line),
    async step(key, producer) {
        if (!journal.has(key)) journal.set(key, JSON.parse(JSON.stringify(await producer())));
        return journal.get(key);
    },
    async parallel(thunks) {
        return Promise.all(thunks.map(async (thunk) => {
            try {
                return await thunk();
            } catch {
                return null;
            }
        }));
    },
    async agent(prompt, options = {}) {
        agents.push({ agent: options.agent, label: options.label });
        const answer = worker(prompt, options);
        const missing = (options.schema?.required ?? []).filter((key) => !(key in answer));
        if (missing.length) throw new Error(`${options.agent} の答えに ${missing.join(", ")} がありません`);
        return answer;
    },
    async workflow() {
        throw new Error("nested workflows are not supported");
    },
};

let outcome;
try {
    outcome = { ok: true, result: await joined.workflows[0].run(ctx) };
} catch (error) {
    outcome = { ok: false, error: error.message };
}
process.stdout.write(JSON.stringify({ ...outcome, phases, logs, agents }));
