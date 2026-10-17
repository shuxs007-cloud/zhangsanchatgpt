import fs from 'node:fs/promises';
import {FileBlob,SpreadsheetFile} from '@oai/artifact-tool';
const [input,output]=process.argv.slice(2);
const w=await SpreadsheetFile.importXlsx(await FileBlob.load(input));
const blob=await w.render({sheetName:'截止日',range:'A1:C5',scale:2,format:'png'});
await fs.writeFile(output,new Uint8Array(await blob.arrayBuffer()));
console.log((await w.inspect({kind:'region',sheetId:'截止日',range:'A1:C5',maxChars:1600})).ndjson);
