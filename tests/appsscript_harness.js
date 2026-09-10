/**
 * appsscript_harness.js — run the REAL Apps Script from sheets.py, offline.
 *
 * The script that reads a sheet is JavaScript living in the spreadsheet, so a
 * Python re-implementation of it proves nothing: the thing that has to be
 * right is the source we tell operators to paste. This runs THAT source, over
 * HTTP, against a stub spreadsheet — so links.read_sheet_via_script talks to
 * the actual script the way it will in production.
 *
 * Usage: node appsscript_harness.js <script.js> <fixture.json> <port>
 *   fixture.json: {"title": "...", "tabs": [{gid, title, hidden, values}]}
 * Prints "READY <port>" on stdout once listening.
 */
const fs = require('fs');
const http = require('http');
const vm = require('vm');

const [, , scriptPath, fixturePath, portArg] = process.argv;
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

function makeSheet(t) {
  const values = t.values || [];
  const rows = values.length;
  const cols = values.reduce((m, r) => Math.max(m, r.length), 0);
  return {
    getSheetId: () => t.gid,
    getName: () => t.title,
    isSheetHidden: () => !!t.hidden,
    getLastRow: () => rows,
    getLastColumn: () => cols,
    getRange: (r, c, nr, nc) => ({
      // Apps Script pads short rows to the rectangle; the REST API omits
      // trailing empties. scan_values tolerates both, but the harness must
      // behave like Apps Script or the test proves the wrong thing.
      getDisplayValues: () => {
        const out = [];
        for (let i = 0; i < nr; i++) {
          const row = values[r - 1 + i] || [];
          const line = [];
          for (let j = 0; j < nc; j++) line.push(String(row[c - 1 + j] ?? ''));
          out.push(line);
        }
        return out;
      },
      getValues() { return this.getDisplayValues(); },
      setValues: () => {},
    }),
  };
}

const book = {
  getName: () => fixture.title,
  getSheets: () => (fixture.tabs || []).map(makeSheet),
  getSheetByName: (n) => {
    const t = (fixture.tabs || []).find((x) => x.title === n);
    return t ? makeSheet(t) : null;
  },
  insertSheet: (n) => makeSheet({ gid: 999, title: n, values: [] }),
};

let lastOutput = null;
const sandbox = {
  SpreadsheetApp: { getActiveSpreadsheet: () => book },
  LockService: { getScriptLock: () => ({ waitLock: () => true, releaseLock: () => {} }) },
  ContentService: {
    MimeType: { JSON: 'application/json' },
    createTextOutput: (s) => { lastOutput = s; return { setMimeType: () => ({ getContent: () => s }) }; },
  },
  Utilities: { sleep: () => {} },
  console,
  JSON, String, Number, Object, Array, Date, Math, parseInt, parseFloat, isNaN,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), sandbox);

http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => { body += d; });
  req.on('end', () => {
    lastOutput = null;
    let out;
    try {
      if (req.method === 'GET') {
        sandbox.doGet();
      } else {
        sandbox.doPost({ postData: { contents: body } });
      }
      out = lastOutput;
    } catch (e) {
      out = JSON.stringify({ error: 'script threw: ' + e.message });
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(out == null ? '{"error":"script returned nothing"}' : out);
  });
}).listen(Number(portArg), '127.0.0.1', function () {
  console.log('READY ' + portArg);
});
