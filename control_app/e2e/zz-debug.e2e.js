import { test, expect } from '@playwright/test';
import { login, nav } from './helpers.js';
test('dbg', async ({ page }) => {
  await login(page); await nav(page, 'Analytics');
  const w = () => page.evaluate(() => { const cw=document.documentElement.clientWidth; const bad=[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>cw+1).slice(0,6).map(e=>e.tagName+'.'+String(e.className).slice(0,30)+' '+Math.round(e.getBoundingClientRect().right)); return [document.documentElement.scrollWidth, bad]; });
  const c = page.locator('section.card').filter({has: page.getByRole('heading',{name:/^Returns/})});
  console.log('start', await w());
  await c.getByRole('radio',{name:'Week'}).click(); await page.waitForTimeout(500); console.log('week', await w());
  await c.getByRole('radio',{name:'Month'}).click(); await page.waitForTimeout(500); console.log('month', await w());
  await c.getByRole('radio',{name:'Day'}).click(); await page.waitForTimeout(500); console.log('day', await w());
  const t = c.getByTestId('returns-bar').nth(20); await t.dispatchEvent('click'); await page.waitForTimeout(300); console.log('select', await w());
  await c.getByRole('button',{name:'Details'}).click(); await page.waitForTimeout(500); console.log('sheet', await w());
  await page.getByTestId('returns-sheet').getByRole('button',{name:'Close'}).click(); await page.waitForTimeout(300); console.log('closed', await w());
  await c.getByRole('button',{name:'Show as table'}).click(); await page.waitForTimeout(300); console.log('table', await w());
  await c.getByRole('button',{name:'Show as bars'}).click(); await page.waitForTimeout(500); console.log('bars', await w());
});
