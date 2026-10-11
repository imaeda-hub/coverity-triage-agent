// Stands in for "@github/copilot-sdk/extension" in the tests: records what the extension registers.
export const joined = [];

export function defineWorkflow(definition) {
    return definition;
}

export async function joinSession(config) {
    joined.push(config);
    return {};
}
