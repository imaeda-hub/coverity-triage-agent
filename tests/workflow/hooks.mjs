// Module hooks: the SDK import of the extension resolves to the test double.
export async function resolve(specifier, context, nextResolve) {
    if (specifier === "@github/copilot-sdk/extension") {
        return { url: new URL("./sdk.mjs", import.meta.url).href, shortCircuit: true };
    }
    return nextResolve(specifier, context);
}
