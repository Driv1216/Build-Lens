// Stage 14 lab-only differential oracle for TS/JS module edges (architecture-evaluation.v1).
// Enumerates module specifiers with ts.createSourceFile and resolves them with
// ts.resolveModuleName under explicit options and a host confined to the archive root. No
// Program, no tsconfig reading, no emit, no plugins; nothing in the archive is executed.
// Usage: node architecture_differential.mjs <typescript-package> <archive-root> <options-json> <file>...
import { createRequire } from "node:module";
import { existsSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const ts = require(process.argv[2]);
const root = path.resolve(process.argv[3]);
const options = ts.convertCompilerOptionsFromJson(JSON.parse(process.argv[4]), root);
if (options.errors.length) throw new Error("invalid compiler options");
const files = process.argv.slice(5);
const ALIAS = ["@/", "~/", "#", "/"];
const CODE = [".ts", ".tsx", ".mts", ".cts", ".d.ts", ".js", ".jsx", ".mjs", ".cjs"];

const inside = (file) => {
  const resolved = path.resolve(file);
  return resolved === root || resolved.startsWith(root + path.sep);
};
const isFile = (file) => inside(file) && existsSync(file) && statSync(file).isFile();
const host = {
  fileExists: isFile,
  readFile: (file) => (isFile(file) ? readFileSync(file, "utf8") : undefined),
  directoryExists: (dir) => inside(dir) && existsSync(dir) && statSync(dir).isDirectory(),
  realpath: (file) => file,
  getCurrentDirectory: () => root,
};

function scriptKind(file) {
  if (file.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (file.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (/\.[cm]?js$/.test(file)) return ts.ScriptKind.JS;
  return ts.ScriptKind.TS;
}

function label(specifier, file) {
  if (ALIAS.some((prefix) => specifier.startsWith(prefix))) {
    return { label: "unknown", targets: [], note: "ALIAS_NOT_RESOLVED_BY_DESIGN" };
  }
  const result = ts.resolveModuleName(specifier, file, options.options, host).resolvedModule;
  if (result && !result.isExternalLibraryImport && inside(result.resolvedFileName)) {
    return { label: "module", targets: [path.relative(root, result.resolvedFileName)] };
  }
  if (!specifier.startsWith(".")) return { label: "external", targets: [] };
  const direct = path.resolve(path.dirname(file), specifier);
  if (!CODE.some((ext) => specifier.endsWith(ext)) && isFile(direct)) {
    return { label: "module", targets: [path.relative(root, direct)], note: "NON_CODE_ASSET" };
  }
  return { label: "none", targets: [] };
}

function analyse(file) {
  const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, scriptKind(file));
  const found = [];
  const add = (node, kind, literal) => found.push({ position: node.getStart(source), kind, specifier: literal.text });
  const visit = (node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      add(node, "import", node.moduleSpecifier);
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
      add(node, "reexport", node.moduleSpecifier);
    } else if (
      ts.isImportEqualsDeclaration(node) &&
      ts.isExternalModuleReference(node.moduleReference) &&
      ts.isStringLiteral(node.moduleReference.expression)
    ) {
      add(node, "import", node.moduleReference.expression);
    } else if (ts.isCallExpression(node) && node.arguments.length >= 1 && ts.isStringLiteral(node.arguments[0])) {
      const dynamic = node.expression.kind === ts.SyntaxKind.ImportKeyword;
      const required = ts.isIdentifier(node.expression) && node.expression.text === "require";
      if (dynamic || required) add(node, "import", node.arguments[0]);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  found.sort((a, b) => a.position - b.position);
  return found.map(({ kind, specifier }) => ({ kind, specifier, ...label(specifier, file) }));
}

process.stdout.write(
  JSON.stringify({ typescript: ts.version, files: Object.fromEntries(files.map((file) => [path.relative(root, file), analyse(file)])) }),
);
