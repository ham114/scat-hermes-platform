if (process.env.SCAT_PORTAL_READBACK !== '1') throw Error('Explicit read-only verification flag required')
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'
import { readFileSync, writeFileSync } from 'node:fs'
let raw='';for await(const chunk of process.stdin)raw+=chunk
let {username,password,key,base='https://172.26.67.148:8444'}=JSON.parse(raw);raw=''
const record=JSON.parse(readFileSync('var/validation/20261108-platform-closeout/natural-acceptance.json','utf8'))
const report={status:'running',url:base,authenticated_public_gateway:base.startsWith('https:'),cases:[],writes:0,bearer_headers_in_browser:0}
let browser
try{
 browser=await chromium.launch({channel:'msedge',headless:true})
 for(const [name,viewport] of [['desktop',{width:1440,height:1000}],['mobile',{width:390,height:844}]]){
  const context=await browser.newContext({viewport,ignoreHTTPSErrors:true,httpCredentials:{username,password}})
  if(base.startsWith('https:')){const r=await context.request.get(base+'/portal-health',{headers:{'X-Portal-Authenticated-User':'other'}});if(r.status()!==200)throw Error('gateway_identity_not_overridden')}
  const page=await context.newPage();const errors=[]
  page.on('pageerror',e=>errors.push(e.name))
  page.on('request',r=>{if(r.headers().authorization?.startsWith('Bearer '))report.bearer_headers_in_browser++})
  await page.route('**/*',route=>{if(!['GET','HEAD'].includes(route.request().method())){report.writes++;return route.abort()}return route.continue()})
  await page.goto(base+'/?thread_id='+record.thread_id)
  const card=page.locator('.task-card').filter({hasText:record.task_id})
  await card.getByRole('button',{name:/^查看预测轨迹/}).click()
  const panel=card.getByRole('region',{name:'结构化分析结果'})
  await panel.locator('header').first().click()
  await page.getByLabel('你的问题',{exact:true}).fill('请将当前结果改成双列。')
  await page.getByRole('button',{name:'发送问题',exact:true}).click()
  await page.waitForFunction(()=>document.querySelector('.task-card .display-grid')?.classList.contains('dual'))
  await page.reload();await card.getByRole('button',{name:/^查看预测轨迹/}).click()
  const scene=panel.locator('.cesium-container').first()
  await page.waitForFunction(()=>document.querySelector('.task-card .cesium-container')?.getAttribute('data-point-count')==='40')
  if(!await panel.locator('.display-grid').evaluate(e=>e.classList.contains('dual')))throw Error('layout_not_restored')
  await panel.getByRole('button',{name:'打开轨迹交互'}).click()
  const workspace=panel.getByRole('region',{name:'轨迹交互工作台',exact:true})
  await workspace.getByRole('button',{name:'切换三维'}).click()
  await page.waitForFunction(()=>[...document.querySelectorAll('.cesium-container')].some(e=>e.getAttribute('data-scene-dimension')==='3d'&&e.getAttribute('data-point-count')==='40'))
  await page.goto(base+'/settings');await page.getByLabel('设置管理凭据',{exact:true}).fill(key)
  const settings=page.waitForResponse(r=>new URL(r.url()).pathname==='/author-api/v1/settings')
  await page.getByRole('button',{name:'连接 / 刷新配置',exact:true}).click()
  if((await settings).status()!==200)throw Error('settings_unavailable')
  await page.getByRole('heading',{name:'后台执行',exact:true}).waitFor()
  await page.getByLabel('设置管理凭据',{exact:true}).fill('')
  await page.goto(base+'/skills');await page.getByLabel('作者凭据',{exact:true}).fill(key)
  const catalog=page.waitForResponse(r=>new URL(r.url()).pathname==='/author-api/v1/skills/catalog')
  await page.getByRole('button',{name:'连接 / 刷新',exact:true}).click()
  const response=await catalog;if(response.status()!==200||(await response.json()).items.length!==12)throw Error('catalog_unavailable')
  await page.getByRole('button',{name:/scat-flight-query/}).waitFor()
  await page.getByLabel('作者凭据',{exact:true}).fill('')
  const leaked=await page.evaluate(secrets=>[...Object.values(localStorage),...Object.values(sessionStorage)].some(v=>secrets.some(s=>s&&v.includes(s))),[password,key])
  if(leaked||errors.length||report.writes||report.bearer_headers_in_browser)throw Error('guard_failed')
  report.cases.push({name,graph_points:40,layout_refresh:true,three_dimensional:true,settings:200,skills:12,page_errors:errors,secrets_in_storage:leaked})
  await context.close()
 }
 report.status='passed'
}catch(error){report.status='failed';report.error_type=error.name;process.exitCode=1}
finally{username='';password='';key='';await browser?.close();writeFileSync('var/validation/20261108-platform-closeout/https-browser.json',JSON.stringify(report,null,2));process.stdout.write(JSON.stringify(report)+'\n')}
