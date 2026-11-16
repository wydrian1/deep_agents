import { expect, test } from '@playwright/test';
import { createAgent, openView, unique } from './helpers';

test('the daily summary shows counts only and never a prompt', async ({ page, request }) => {
  const secret = unique('SECRETPROMPT');
  const started = await request.post('/api/runs', { data: { prompt: secret, mode: 'test' } });
  expect(started.status()).toBe(201);
  await openView(page, 'Today');
  await expect(page.getByRole('heading', { name: 'What happened today' })).toBeVisible();
  await expect(page.locator('.daily-summary-grid article').first()).toContainText('Workflows');
  await expect(page.locator('.daily-summary-grid article').nth(1)).toContainText('Standalone agent runs');
  await expect(page.locator('.daily-summary')).not.toContainText(secret);
});

test('the language switch translates the navigation and the choice is remembered', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Switch language|Nyelv/ }).click();
  const nav = page.locator('.studio-navigation');
  await expect(nav).toContainText('Tudástár');
  await expect(nav).toContainText('Workflow-k');
  await page.reload();
  await expect(page.locator('.studio-navigation')).toContainText('Tudástár');
  await page.getByRole('button', { name: /Nyelv|Switch language/ }).click(); // back to English for the next tests
  await expect(page.locator('.studio-navigation')).toContainText('Knowledge base');
});

test('a saved agent is listed in the library and a test-mode run completes in the workspace', async ({ page, request }) => {
  const name = unique('agent');
  await createAgent(request, name);
  await openView(page, 'Agent library');
  await expect(page.getByText(name).first()).toBeVisible();
  const prompt = unique('hello');
  const started = await request.post('/api/runs', { data: { prompt, mode: 'test' } });
  expect(started.status()).toBe(201);
  await openView(page, 'Workspace');
  const item = page.locator('.studio-run-item', { hasText: prompt });
  await expect(item).toBeVisible();
  await item.click();
  await expect(page.locator('.studio-run-heading .studio-badge')).toContainText('Completed');
});

test('no horizontal overflow on a phone-sized screen in any view', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  for (const view of ['Workflows', 'Knowledge base', 'Today', 'Schedules']) {
    await openView(page, view);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, view).toBeLessThanOrEqual(0);
  }
});
