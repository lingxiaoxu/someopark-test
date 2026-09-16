import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

function source(path: string) {
  return ts.createSourceFile(path, readFileSync(new URL(`../${path}`, import.meta.url), 'utf8'),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}

function find(root: ts.Node, predicate: (node: ts.Node) => boolean): ts.Node {
  let found: ts.Node | undefined;
  const visit = (node: ts.Node) => {
    if (found) return;
    if (predicate(node)) found = node;
    else ts.forEachChild(node, visit);
  };
  visit(root);
  assert.ok(found, 'expected application source node is missing');
  return found;
}

function initializer(root: ts.SourceFile, name: string): ts.Expression {
  const declaration = find(root, node => ts.isVariableDeclaration(node) &&
    ts.isIdentifier(node.name) && node.name.text === name) as ts.VariableDeclaration;
  assert.ok(declaration.initializer, `${name} has no initializer`);
  return declaration.initializer;
}

function callback(root: ts.SourceFile, name: string): ts.Expression {
  const call = initializer(root, name);
  assert.ok(ts.isCallExpression(call), `${name} must remain a hook callback`);
  return call.arguments[0];
}

function evaluate(expression: string, globals: Record<string, unknown> = {}): any {
  const compiled = ts.transpileModule(`globalThis.result = (${expression});`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText;
  const context = { ...globals, result: undefined as any };
  runInNewContext(compiled, context, { timeout: 1000 });
  return context.result;
}

const app = source('src/App.tsx');
const chat = source('src/components/ChatArea.tsx');
const prediction = source('src/components/prediction/PredictionArtifact.tsx');
const crypto = source('src/crypto-markets/CryptoPanelContent.tsx');
const cryptoArtifacts = source('src/crypto-markets/Artifacts.tsx');
const isPredictionArtifact = evaluate(initializer(prediction, 'isPredictionArtifact').getText(prediction));

// Execute the real classification helpers without loading React, charts or APIs.
// Only the unused rendering fields/icons are omitted from the actual registry.
const registry = initializer(cryptoArtifacts, 'ARTIFACTS');
assert.ok(ts.isArrayLiteralExpression(registry));
const ARTIFACTS = registry.elements.map(item => {
  const property = find(item, node => ts.isPropertyAssignment(node) && node.name.getText(cryptoArtifacts) === 'type') as ts.PropertyAssignment;
  return { type: evaluate(property.initializer.getText(cryptoArtifacts)) };
});
function cryptoFunction(name: string, globals: Record<string, unknown>) {
  const declaration = find(crypto, node => ts.isFunctionDeclaration(node) && node.name?.text === name);
  return evaluate(declaration.getText(crypto).replace(/^export\s+/, ''), globals);
}
const artifactType = cryptoFunction('artifactType', {
  PREFIX: evaluate(initializer(crypto, 'PREFIX').getText(crypto)), ARTIFACTS,
});
const isCryptoArtifact = cryptoFunction('isCryptoArtifact', { artifactType });

function access(mode: string, signedIn = false) {
  const opened: unknown[] = [];
  const dialogs: boolean[] = [];
  const open = evaluate(callback(app, 'setModeArtifact').getText(app), {
    appModeRef: { current: mode }, session: signedIn ? { user: { id: 'test-user' } } : null,
    isPredictionArtifact, isCryptoArtifact,
    setActiveArtifact: (artifact: unknown) => opened.push(artifact),
    setIsAuthDialogOpen: (value: boolean) => dialogs.push(value),
  });
  return { open, opened, dialogs };
}

const privateArtifacts = [
  ['stock', 'inventory'], ['macro', 'macro_inflation'],
  ['soccer', 'soccer_league_table'], ['crypto', 'crypto_performance'],
];

test('anonymous stock, macro, soccer and crypto artifact clicks use the sign-in dialog', () => {
  for (const [mode, type] of privateArtifacts) {
    const state = access(mode);
    state.open({ type, title: type });
    assert.deepEqual(state.opened, [], mode);
    assert.deepEqual(state.dialogs, [true], mode);
  }
});

test('World Cup artifacts stay public and authenticated artifacts retain their parameters', () => {
  for (const [mode, type, signedIn] of [
    ['prediction', 'wc_champion', false],
    ...privateArtifacts.map(([mode, type]) => [mode, type, true]),
  ] as Array<[string, string, boolean]>) {
    const state = access(mode, signedIn);
    const artifact = { type, title: type, params: { strategy: 'mrpt', ticker: 'test-contract' } };
    state.open(artifact);
    assert.equal(state.opened.length, 1, mode);
    assert.strictEqual(state.opened[0], artifact, mode);
    assert.deepEqual(state.dialogs, [], mode);
  }
});

test('closing a panel needs no login and crypto cross-mode isolation remains enforced', () => {
  for (const mode of ['stock', 'prediction', 'macro', 'soccer', 'crypto']) {
    const state = access(mode);
    state.open(null);
    assert.deepEqual(state.opened, [null], mode);
    assert.deepEqual(state.dialogs, [], mode);
  }
  for (const signedIn of [false, true]) {
    for (const [mode, type] of [['crypto', 'wc_champion'], ['crypto', 'inventory'], ['stock', 'crypto_performance']]) {
      const state = access(mode, signedIn);
      state.open({ type });
      assert.deepEqual(state.opened, [], `${mode}: ${type}`);
    }
  }
});

test('context navigation and the reopen button both use the guarded application entry', () => {
  const provider = find(app, node => ts.isJsxOpeningElement(node) && node.tagName.getText(app) === 'ArtifactProvider') as ts.JsxOpeningElement;
  const value = find(provider.attributes, node => ts.isJsxAttribute(node) && node.name.getText(app) === 'value') as ts.JsxAttribute;
  assert.ok(value.initializer && ts.isJsxExpression(value.initializer));
  assert.equal(value.initializer.expression?.getText(app), 'setModeArtifact');
  const reopen = find(app, node => ts.isJsxElement(node) && node.openingElement.tagName.getText(app) === 'button' &&
    node.openingElement.attributes.properties.some(property => ts.isJsxAttribute(property) &&
      property.name.getText(app) === 'title' && property.initializer && ts.isStringLiteral(property.initializer) &&
      property.initializer.text === 'Reopen panel')) as ts.JsxElement;
  const click = find(reopen.openingElement.attributes, node => ts.isJsxAttribute(node) && node.name.getText(app) === 'onClick') as ts.JsxAttribute;
  assert.ok(click.initializer && ts.isJsxExpression(click.initializer) && click.initializer.expression);
  for (const signedIn of [false, true]) {
    const state = access('crypto', signedIn);
    const artifact = { type: 'crypto_performance' };
    const onClick = evaluate(click.initializer.expression.getText(app), {
      lastClosedArtifact: artifact, setModeArtifact: state.open, setLastClosedArtifact: () => {},
      setActiveArtifact: () => assert.fail('reopen must not bypass the guarded entry'),
    });
    onClick();
    assert.equal(state.opened.length, signedIn ? 1 : 0);
    assert.deepEqual(state.dialogs, signedIn ? [] : [true]);
  }
});

test('anonymous chat submission requires login before ordinary/coding or Agent branching', async () => {
  for (const isAgentMode of [false, true]) {
    let dialogs = 0;
    let prevented = 0;
    const submit = evaluate(callback(chat, 'handleSubmit').getText(chat), {
      session: null, isAgentMode, input: 'Build a small app', isLoading: false,
      onSignInClick: () => dialogs++,
      handleAgentSubmit: () => assert.fail('anonymous Agent request'),
      setMessages: () => assert.fail('anonymous chat request'),
      fetch: () => assert.fail('anonymous network request'),
    });
    await submit({ preventDefault: () => prevented++ });
    assert.equal(dialogs, 1);
    assert.equal(prevented, 1);
  }
});
