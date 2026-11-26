import { pathToFileURL } from 'node:url';
const { chromium } = await import(pathToFileURL(process.env.PLANNER_FRONTEND_ROOT + '/node_modules/playwright/index.mjs').href);
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';

const api = 'http://127.0.0.1:8017/api';
const origin = 'http://127.0.0.1:5179';
const directory = new URL('../docs/evaluations/2026-11-26/constraints/browser/', import.meta.url);
await mkdir(directory, { recursive: true });
const login = await fetch(`${api}/auth/login/`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: process.env.PLANNER_QA_USER || 'runtime-qa', password: 'Local-QA-only-2026!' }) });
assert.equal(login.status, 200);
const account = await login.json();
const authorization = { Authorization: `Bearer ${account.tokens.access}` };
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, deviceScaleFactor: 1 });
await context.addInitScript(data => {
  localStorage.setItem('access_token', data.tokens.access);
  localStorage.setItem('refresh_token', data.tokens.refresh);
  localStorage.setItem('auth-storage', JSON.stringify({ state: { user: data.user, token: data.tokens.access, isAuthenticated: true }, version: 0 }));
  localStorage.setItem('planner-onboarding', JSON.stringify({ state: { hasCompletedOnboarding: true }, version: 0 }));
}, account);
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const requests = [];
page.on('request', request => {
  if (request.url().startsWith(api) && request.method() === 'POST') requests.push(new URL(request.url()).pathname);
});
async function send(text) {
  const input = page.getByPlaceholder('Écris un message…');
  await input.fill(text);
  await page.getByRole('button', { name: 'Envoyer le message', exact: true }).click();
}
try {
  const conversationResponse = await fetch(`${api}/conversations/`, { method: 'POST', headers: { ...authorization, 'Content-Type': 'application/json' }, body: JSON.stringify({ client_runtime: 'v3', titre: 'QA navigateur durable' }) });
  const conversation = await conversationResponse.json();
  assert.equal(conversation.engine_version, 'v3');
  await page.goto(`${origin}/chat`);
  await page.getByPlaceholder('Écris un message…').waitFor({ timeout: 30000 });
  await send('Planifie Marché de Noël demain de 11h40 à 13h.');
  await page.getByRole('region', { name: 'Question de Planner' }).waitFor({ timeout: 60000 });
  await page.screenshot({ path: new URL('question-mobile.png', directory).pathname.replace(/^\/([A-Za-z]:)/, '$1'), fullPage: true });
  await page.reload();
  const question = page.getByRole('region', { name: 'Question de Planner' });
  await question.waitFor({ timeout: 30000 });
  const free = question.getByRole('button', { name: 'Répondre autrement' });
  if (await free.isVisible()) await free.click();
  await question.getByLabel('Ta réponse').fill('Le même événement de 11h40 à 12h40. Conserve le cours et le transport.');
  const extraRequired = 0;
  await question.getByRole('button', { name: 'Envoyer ma réponse' }).click();
  await page.getByText('Action vérifiée', { exact: true }).first().waitFor({ timeout: 60000 });
  await page.getByRole('region', { name: 'Question de Planner' }).waitFor({ state: 'hidden', timeout: 10000 });
  await page.getByRole('button', { name: 'Arrêter cette demande' }).waitFor({ state: 'hidden', timeout: 60000 });
  await page.screenshot({ path: new URL('receipt-mobile.png', directory).pathname.replace(/^\/([A-Za-z]:)/, '$1'), fullPage: true });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
  assert.equal(overflow, false, 'horizontal overflow on 390px viewport');
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({ path: new URL('receipt-desktop.png', directory).pathname.replace(/^\/([A-Za-z]:)/, '$1'), fullPage: true });
  assert.equal(requests.filter(path => path === '/api/chat/runs/').length, 1, 'reload must not resubmit user prompt');
  assert.equal(requests.filter(path => path.includes('/answer/')).length, 1, 'one typed answer only');
  await page.reload();
  await page.getByText('Action vérifiée', { exact: true }).first().waitFor({ timeout: 30000 });
  await page.getByPlaceholder('Écris un message…').fill('Merci, bonne journée.');
  await page.getByRole('button', { name: 'Envoyer le message', exact: true }).click();
  await page.getByRole('button', { name: 'Arrêter cette demande' }).waitFor({ timeout: 30000 });
  await page.getByRole('button', { name: 'Arrêter cette demande' }).waitFor({ state: 'hidden', timeout: 60000 });
  assert.equal(requests.filter(path => path === '/api/conversations/').length, 0, 'reload must keep the current conversation');
  assert.equal(requests.filter(path => path === '/api/chat/runs/').length, 2);
  assert.deepEqual(errors, []);
  const provider = process.env.PLANNER_QA_CONTROLLED === '1' ? 'controlled' : 'DeepSeek';
  await writeFile(new URL('result.json', directory), JSON.stringify({ passed: true, provider, terminal: true, sameConversationAfterReload: true, viewport: [390, 844], extraRequired, answerMode: 'free', requests, errors }, null, 2));
  console.log(`PASS browser (${provider}): persisted question, answer, receipt, terminal, reload, same conversation, mobile width.`);
} catch (error) {
  await page.screenshot({ path: new URL('failure.png', directory).pathname.replace(/^\/([A-Za-z]:)/, '$1'), fullPage: true });
  await writeFile(new URL('failure.txt', directory), `${error.stack}\n\n${await page.locator('body').innerText()}`);
  throw error;
} finally { await browser.close(); }
