// Lab-only TypeScript 5.7.3 call oracle. Parse/type-check only: no emit, repository execution,
// config plugins, package installation, or compiler-host reads outside the archived tree.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const ts = require(path.resolve(process.argv[2]));
if (ts.version !== '5.7.3') throw Error(`Expected TypeScript 5.7.3, got ${ts.version}`);
const root = fs.realpathSync(process.argv[3]);
const files = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const options = { noEmit: true, noLib: true, allowJs: true, checkJs: true,
  skipLibCheck: true, target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext,
  moduleResolution: ts.ModuleResolutionKind.Bundler, jsx: ts.JsxEmit.Preserve,
  allowImportingTsExtensions: true, resolveJsonModule: false };
const inside = p => {
  const resolved = fs.existsSync(p) ? fs.realpathSync(p) : path.resolve(p);
  return resolved === root || resolved.startsWith(root + path.sep);
};
const host = ts.createCompilerHost(options);
host.readFile = p => inside(p) && fs.existsSync(p) && fs.lstatSync(p).isFile() && !fs.lstatSync(p).isSymbolicLink()
  ? fs.readFileSync(p, 'utf8') : undefined;
host.fileExists = p => inside(p) && fs.existsSync(p) && fs.lstatSync(p).isFile() && !fs.lstatSync(p).isSymbolicLink();
host.directoryExists = p => inside(p) && fs.existsSync(p) && fs.lstatSync(p).isDirectory() && !fs.lstatSync(p).isSymbolicLink();
host.getSourceFile = (p, version) => {
  const text = host.readFile(p);
  return text === undefined ? undefined : ts.createSourceFile(p, text, version, true);
};
const program = ts.createProgram(files.map(f => path.join(root, f)), options, host);
const checker = program.getTypeChecker();
const calls = [], functions = [];
const functionLike = n => ts.isFunctionDeclaration(n) || ts.isFunctionExpression(n) ||
  ts.isArrowFunction(n) || ts.isMethodDeclaration(n) || ts.isConstructorDeclaration(n);
const nameOf = n => n.name?.getText() ||
  (ts.isVariableDeclaration(n.parent) ? n.parent.name.getText() :
   ts.isPropertyAssignment(n.parent) ? n.parent.name.getText() : '<anonymous>');
const target = n => {
  if (!n || !functionLike(n) || !n.body) return null;
  const sf = n.getSourceFile();
  if (!inside(sf.fileName)) return null;
  return { locator: path.relative(root, sf.fileName),
    line: sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1, name: nameOf(n) };
};
for (const locator of files) {
  const sf = program.getSourceFile(path.join(root, locator));
  if (!sf) continue;
  const visit = (node, scope) => {
    let owner = scope;
    if (functionLike(node) && node.body) {
      owner = target(node);
      functions.push({ ...owner, end_line: sf.getLineAndCharacterOfPosition(node.end).line + 1 });
    }
    if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
      const expr = node.expression;
      const start = sf.getLineAndCharacterOfPosition(node.getStart(sf));
      const decl = checker.getResolvedSignature(node)?.declaration;
      const resolved = target(decl);
      calls.push({ locator, line: start.line + 1,
        column: Buffer.byteLength(sf.text.slice(sf.getPositionOfLineAndCharacter(start.line, 0), node.getStart(sf)), 'utf8'),
        oracle_column: sf.getLineAndCharacterOfPosition(expr.end).character,
        end_line: sf.getLineAndCharacterOfPosition(node.end).line + 1,
        end_column: Buffer.byteLength(sf.text.slice(
          sf.getPositionOfLineAndCharacter(sf.getLineAndCharacterOfPosition(node.end).line, 0),
          node.end), 'utf8'),
        language: 'typescript', form: ts.isIdentifier(expr) ? 'direct' :
          ts.isPropertyAccessExpression(expr) ? 'attribute' : 'dynamic',
        callee: expr.getText(sf), owner,
        oracle: { state: resolved ? 'internal' : 'unknown', targets: resolved ? [resolved] : [],
          note: resolved ? 'TypeScript resolved signature declaration; requires source adjudication' :
            'No local callable declaration from compiler; not evidence of externality' } });
    }
    ts.forEachChild(node, child => visit(child, owner));
  };
  visit(sf, null);
}
process.stdout.write(JSON.stringify({ version: ts.version, calls, functions }));
