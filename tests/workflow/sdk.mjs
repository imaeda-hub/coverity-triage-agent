// Test double for "@github/copilot-sdk/extension", which the Copilot CLI provides to extensions.
// It only records what the extension registers.
export const sessions = [];

export function defineWorkflow(definition) {
    return definition;
}

export async function joinSession(config) {
    sessions.push(config);
    return {};
}
