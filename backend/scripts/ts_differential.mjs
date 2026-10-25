// Stage 13 lab-only differential: declarations seen by the TypeScript compiler's *parser*.
// Uses ts.createSourceFile only (no Program, no tsconfig, no module resolution, nothing executed).
// Usage: node ts_differential.mjs <path-to-typescript-package> <file>...  -> JSON on stdout.
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const ts = require(process.argv[2]);
const files = process.argv.slice(3);

const kindOf = (node, inClass) => {
  if (ts.isFunctionDeclaration(node)) return "function";
  if (ts.isClassDeclaration(node) || ts.isClassExpression(node)) return "class";
  if (ts.isMethodDeclaration(node) || ts.isMethodSignature(node)) return inClass ? "method" : "function";
  if (ts.isInterfaceDeclaration(node)) return "interface";
  if (ts.isTypeAliasDeclaration(node)) return "type_alias";
  if (ts.isEnumDeclaration(node)) return "enum";
  if (ts.isModuleDeclaration(node)) return "namespace";
  return null;
};

const isFunctionValue = (node) =>
  node && (ts.isArrowFunction(node) || ts.isFunctionExpression(node));

const unwrap = (node) => {
  if (node && ts.isCallExpression(node) && node.arguments.length === 1) {
    const callee = node.expression.getText();
    if (["memo", "forwardRef", "React.memo", "React.forwardRef"].includes(callee)) {
      return isFunctionValue(node.arguments[0]) ? node.arguments[0] : null;
    }
  }
  return isFunctionValue(node) ? node : null;
};

function analyse(file) {
  const text = readFileSync(file, "utf8");
  const kind = file.endsWith(".tsx")
    ? ts.ScriptKind.TSX
    : file.endsWith(".ts")
      ? ts.ScriptKind.TS
      : file.endsWith(".jsx")
        ? ts.ScriptKind.JSX
        : ts.ScriptKind.JS;
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const declarations = new Set();
  const imports = new Set();
  const visit = (node, prefix, inClass) => {
    let name = null;
    let declared = kindOf(node, inClass);
    if (declared && node.name) name = ts.isStringLiteral(node.name) ? node.name.text : node.name.getText();
    if (declared && !node.name && (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node))) name = "default";
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && unwrap(node.initializer)) {
      declared = inClass ? "method" : "function";
      name = node.name.text;
    }
    if (ts.isPropertyDeclaration(node) && inClass && isFunctionValue(node.initializer)) {
      declared = "method";
      name = node.name.getText();
    }
    if (ts.isExportAssignment(node) && !node.isExportEquals && unwrap(node.expression)) {
      declared = "function";
      name = "default";
    }
    let next = prefix;
    let nextInClass = inClass;
    if (declared && name !== null) {
      const qualified = prefix ? `${prefix}.${name}` : name;
      declarations.add(`${declared} ${qualified}`);
      next = qualified;
      nextInClass = declared === "class";
    }
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      imports.add(node.moduleSpecifier.text);
    }
    if (ts.isCallExpression(node) && node.arguments.length === 1 && ts.isStringLiteral(node.arguments[0])) {
      if (node.expression.kind === ts.SyntaxKind.ImportKeyword || node.expression.getText() === "require") {
        imports.add(node.arguments[0].text);
      }
    }
    if (ts.isInterfaceDeclaration(node)) return; // members are not symbols
    ts.forEachChild(node, (child) => visit(child, next, nextInClass && !declared ? inClass : nextInClass));
  };
  visit(source, "", false);
  return {
    file,
    parse_diagnostics: source.parseDiagnostics.length,
    declarations: [...declarations].sort(),
    imports: [...imports].sort(),
  };
}

process.stdout.write(JSON.stringify(files.map(analyse)));
