import { expect, test } from '@playwright/test';
import { openView, unique } from './helpers';

test('an entry is created, changed, restored from its history and deleted', async ({ page }) => {
  const key = unique('notes').toLowerCase();
  await openView(page, 'Knowledge base');
  await page.getByRole('button', { name: 'New entry' }).click();
  await page.getByLabel('Name', { exact: true }).fill(key);
  await page.getByRole('textbox', { name: 'Text', exact: true }).fill('first text');
  await page.getByRole('button', { name: 'Save new revision' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Saved as a new revision.' })).toBeVisible();
  await expect(page.getByRole('button', { name: new RegExp(key) })).toContainText('revision 1');

  await page.getByRole('textbox', { name: 'Text', exact: true }).fill('second text');
  await page.getByRole('button', { name: 'Save new revision' }).click();
  await expect(page.getByRole('button', { name: new RegExp(key) })).toContainText('revision 2');

  await page.getByText(/History \(/).click();
  await page.locator('.memory-editor').getByText(/revision 1 ·/).click();
  await expect(page.locator('.memory-editor pre', { hasText: 'first text' })).toBeVisible();
  await page.getByRole('button', { name: 'Use this text' }).click();
  await expect(page.getByRole('textbox', { name: 'Text', exact: true })).toHaveValue('first text');
  await page.getByRole('button', { name: 'Save new revision' }).click();
  await expect(page.getByRole('button', { name: new RegExp(key) })).toContainText('revision 3'); // restore = a NEW revision

  page.once('dialog', (dialog) => void dialog.accept());
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Entry deleted' })).toBeVisible();
  await expect(page.getByRole('button', { name: new RegExp(key) })).toHaveCount(0); // hidden unless "Show deleted"
  await page.getByLabel('Show deleted').check();
  await expect(page.getByRole('button', { name: new RegExp(key) })).toContainText('deleted');
});

test('a stale save is a conflict that never overwrites the other edit', async ({ page, request }) => {
  const key = unique('conflict').toLowerCase();
  expect((await request.put(`/api/memory/${key}`, { data: { value: 'v1', expected_revision: 0, request_id: unique('r') } })).status()).toBe(200);
  await openView(page, 'Knowledge base');
  await page.getByRole('button', { name: new RegExp(key) }).click();
  await expect(page.getByRole('textbox', { name: 'Text', exact: true })).toHaveValue('v1');
  // Someone else (an agent) writes revision 2 while this editor still shows revision 1.
  expect((await request.put(`/api/memory/${key}`, { data: { value: 'other edit', expected_revision: 1, request_id: unique('r') } })).status()).toBe(200);
  await page.getByRole('textbox', { name: 'Text', exact: true }).fill('my edit');
  await page.getByRole('button', { name: 'Save new revision' }).click();
  await expect(page.getByRole('alert')).toContainText('changed in the meantime');
  await page.getByRole('button', { name: 'Reload the entry' }).click();
  await expect(page.getByRole('textbox', { name: 'Text', exact: true })).toHaveValue('other edit');
});

test('the name rule is enforced before saving', async ({ page }) => {
  await openView(page, 'Knowledge base');
  await page.getByRole('button', { name: 'New entry' }).click();
  await page.getByLabel('Name', { exact: true }).fill('Bad Name!');
  await page.getByRole('textbox', { name: 'Text', exact: true }).fill('x');
  await expect(page.getByRole('button', { name: 'Save new revision' })).toBeDisabled();
});
