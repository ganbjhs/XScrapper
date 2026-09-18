import { parseIgIds } from "./parseIgIds.js";
let fail = 0;
const eq = (name, got, want) => {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a !== b) { console.log("FAIL", name, "\n  got ", a, "\n  want", b); fail++; }
  else console.log("ok  ", name);
};
const P = (t) => parseIgIds(t).pairs;
const B = (t) => parseIgIds(t).bad;

eq("json object", P('{"natgeo": "787132", "nasa": 528817151}'),
   [{handle:"natgeo",id:"787132"},{handle:"nasa",id:"528817151"}]);
eq("json array records", P('[{"handle":"natgeo","profile_id":"787132"},{"username":"nasa","id":528817151}]'),
   [{handle:"natgeo",id:"787132"},{handle:"nasa",id:"528817151"}]);
eq("json array pairs", P('[["natgeo","787132"],["nasa",528817151]]'),
   [{handle:"natgeo",id:"787132"},{handle:"nasa",id:"528817151"}]);
eq("json obj of obj", P('{"natgeo":{"profile_id":"787132"}}'), [{handle:"natgeo",id:"787132"}]);
eq("space", P("natgeo 787132\nnasa 528817151"),
   [{handle:"natgeo",id:"787132"},{handle:"nasa",id:"528817151"}]);
eq("csv", P("natgeo,787132\nnasa,528817151"),
   [{handle:"natgeo",id:"787132"},{handle:"nasa",id:"528817151"}]);
eq("colon", P("natgeo:787132"), [{handle:"natgeo",id:"787132"}]);
eq("tab", P("natgeo\t787132"), [{handle:"natgeo",id:"787132"}]);
eq("pipe", P("natgeo | 787132"), [{handle:"natgeo",id:"787132"}]);
eq("dash + at", P("@natgeo - 787132"), [{handle:"natgeo",id:"787132"}]);
eq("bullet", P("- natgeo 787132"), [{handle:"natgeo",id:"787132"}]);
eq("reversed", P("787132 natgeo"), [{handle:"natgeo",id:"787132"}]);
eq("url", P("https://www.instagram.com/natgeo/  787132"), [{handle:"natgeo",id:"787132"}]);
eq("url colon sep", P("https://www.instagram.com/natgeo/: 787132"), [{handle:"natgeo",id:"787132"}]);
eq("jsonish fragment lines", P('"natgeo": 787132,\n"nasa": 528817151'),
   [{handle:"natgeo",id:"787132"},{handle:"nasa",id:"528817151"}]);
eq("comment+blank skipped", P("# ids\n\nnatgeo 787132\n// x"), [{handle:"natgeo",id:"787132"}]);
eq("dots underscores", P("nat.geo_1 787132"), [{handle:"nat.geo_1",id:"787132"}]);

// must refuse
eq("handle only -> bad", P("natgeo"), []);
eq("handle only listed", B("natgeo"), ["natgeo"]);
eq("non numeric id -> bad", P("natgeo abcdef"), []);
eq("empty", P(""), []);
eq("broken json falls through to lines", P('{"natgeo": 787132'), [{handle:"natgeo",id:"787132"}]);
eq("json bad entry reported", B('{"natgeo": "not-an-id"}'), ["natgeo: not-an-id"]);

console.log(fail ? `\n${fail} FAILED` : "\nall passed");
process.exit(fail ? 1 : 0);
