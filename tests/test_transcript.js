// Exercise bestTranscript / digitsFromWords in isolation.
const NUMBER_WORDS={zero:'0',oh:'0',o:'0',one:'1',two:'2',three:'3',four:'4',
  five:'5',six:'6',seven:'7',eight:'8',nine:'9',double:'',triple:''};
function digitsFromWords(s){
  return s.toLowerCase().split(/[\s,-]+/).map(w=>w in NUMBER_WORDS?NUMBER_WORDS[w]:w).join('');
}
let STATE={slots:[]};
function bestTranscript(result){
  const alts=[...result].map(a=>a.transcript.trim()).filter(Boolean);
  if(!alts.length) return '';
  const needsNumber=!(STATE.slots||[]).includes('contact_details');
  if(needsNumber){
    const full=alts.find(a=>{
      const d=digitsFromWords(a).replace(/\D/g,'');
      return d.length>=10 && d.length<=13;
    });
    if(full) return full;
  }
  return alts[0];
}
const R=(...t)=>t.map(x=>({transcript:x}));
let pass=0, fail=0;
const check=(n,got,want)=>{ const ok=got===want; ok?pass++:fail++;
  console.log(`  ${ok?'PASS':'FAIL'}  ${n}${ok?'':`  got ${JSON.stringify(got)} want ${JSON.stringify(want)}`}`); };

console.log("\n=== the failure from the screenshot ===");
STATE={slots:['customer_name']};
check("picks the complete number over the truncated top result",
  bestTranscript(R("03124 567","0312 4567 890","03019052602")), "0312 4567 890");

console.log("\n=== spoken digits ===");
check("words become a usable number",
  bestTranscript(R("oh three one two four five six seven eight nine")),
  "oh three one two four five six seven eight nine");
check("  ...and digitsFromWords extracts it",
  digitsFromWords("oh three one two four five six seven eight nine"), "03124567 89".replace(" ",""));

console.log("\n=== once the number is known, top result wins again ===");
STATE={slots:['customer_name','contact_details']};
check("no number hunting after contact_details is captured",
  bestTranscript(R("Corolla","0300 1234567")), "Corolla");

console.log("\n=== degenerate input ===");
STATE={slots:[]};
check("empty alternatives", bestTranscript(R()), "");
check("single alternative passes through", bestTranscript(R("Premium Wash")), "Premium Wash");

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail?1:0);
