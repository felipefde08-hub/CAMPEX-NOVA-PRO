import { fileURLToPath } from 'node:url';
const { chromium } = await import(process.env.CAMPEX_PLAYWRIGHT_MODULE || 'playwright');
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('../frontend', import.meta.url));
const calls=[];
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZkAAAAASUVORK5CYII=','base64');
const api=createServer((req,res)=>{
 res.setHeader('Access-Control-Allow-Origin','http://127.0.0.1:5512');
 res.setHeader('Access-Control-Allow-Headers','*');
 res.setHeader('Access-Control-Allow-Methods','GET');
 if(req.method==='OPTIONS'){res.end();return;}
 calls.push({url:req.url,token:req.headers['x-campex-token'],range:req.headers.range});
 if(req.headers['x-campex-token']!=='browser-test-only'){res.writeHead(401);res.end();return;}
 if(req.headers.range){res.writeHead(206,{'Content-Range':'bytes 0-3/10'});res.end('test');return;}
 res.writeHead(200,{'Content-Type':'image/png'});res.end(png);
});
const site=createServer(async(req,res)=>{
 try {
 if(req.url==='/') {
  res.setHeader('Content-Type','text/html');
  res.end(`<script>window.CAMPEX_API_BASE_URL='http://127.0.0.1:5513/api/v1';sessionStorage.setItem('campex.api_token','browser-test-only');</script><script type="module">import * as api from '/js/api.js';window.api=api;</script>`);return;
 }
 const file=await readFile(root+new URL(req.url,'http://local').pathname);
 res.setHeader('Content-Type','application/javascript');res.end(file);
 } catch {res.writeHead(404);res.end();}
});
await Promise.all([new Promise(r=>api.listen(5513,'127.0.0.1',r)),new Promise(r=>site.listen(5512,'127.0.0.1',r))]);
const browser=await chromium.launch({channel:'chrome',headless:true});
try {
 const page=await browser.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:5512/');
 await page.waitForFunction(()=>!!window.api);
 const result=await page.evaluate(async()=>{
  const src=api.cameraSnapshotUrl('test');
  await new Promise((resolve,reject)=>{const image=new Image();image.onload=resolve;image.onerror=()=>reject(Error('image failed'));image.src=src;document.body.append(image);});
  const response=await fetch(api.cameraVideoUrl('test'),{headers:{Range:'bytes=0-3'}});
  const denied=new URL(src);denied.searchParams.set('url','http://127.0.0.1:5513/api/v1/cameras');
  const forbidden=await fetch(denied);
  return {src,status:response.status,text:await response.text(),forbidden:forbidden.status};
 });
 assert.equal(result.status,206);assert.equal(result.text,'test');assert.equal(result.forbidden,502);
 assert.ok(!result.src.includes('browser-test-only'));
 assert.equal(calls.length,2);assert.equal(calls[0].token,'browser-test-only');assert.equal(calls[1].range,'bytes=0-3');
 assert.deepEqual(errors,[]);
 console.log('PASS: native image authentication, video Range, destination restriction, token absent from URL.');
} finally {await browser.close();site.closeAllConnections();api.closeAllConnections();await Promise.all([new Promise(r=>site.close(r)),new Promise(r=>api.close(r))]);}
