// Loads the installed extension with the SDK stub and either prints its registration ("meta") or runs
// the workflow ("run") with a context that behaves like the documented WorkflowContext. The worker
// subagent is played by writing what it would write: an edit in the fix copy and result.json.
//
// Usage: node harness.mjs <config.json>
//   config: { extension, mode, args?, result?, bad_first?: [item ids] }
import { readFileSync, writeFileSync, appendFileSync } from "node:fs";
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

function worker(prompt, options) {
    const brief = readFileSync(/指示ファイル (.+?) を読み/.exec(prompt)[1], "utf8");
    const item = /^# 調査の指示: ([^\r\n]+)/m.exec(brief)[1];
    const fixDir = /\| 修正するコード（ここだけを変更する） \| `(.+?)` \|/.exec(brief)[1];
    const resultFile = /\| 結果ファイル（書く） \| `(.+?)` \|/.exec(brief)[1];
    if (!options.label.includes("-fix")) appendFileSync(join(fixDir, "src", "reader.c"), `/* fix ${item} */\n`);
    const broken = badFirst.has(item) && !options.label.includes("-fix");
    writeFileSync(resultFile, JSON.stringify({ ...config.result, item: broken ? "wrong" : item }));
    return `${item}: 修正を推奨（確信度 高）`;
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
        agents.push(options);
        return worker(prompt, options);
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
