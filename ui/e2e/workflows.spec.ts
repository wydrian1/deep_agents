import { expect, test } from '@playwright/test';
import { createAgent, createApprovalWorkflow, openView, unique } from './helpers';

test('an approval workflow runs from the UI: start, approve, completed', async ({ page, request }) => {
  const name = unique('flow');
  await createApprovalWorkflow(request, name, await createAgent(request, unique('agent')));
  await openView(page, 'Workflows');
  await page.getByRole('button', { name: new RegExp(name) }).click();
  await page.getByRole('button', { name: 'Start', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('textbox').fill('Hello workflow');
  await expect(dialog.getByRole('button', { name: 'Start' })).toBeDisabled(); // needs the data-flow consent
  await dialog.getByRole('checkbox').check();
  await dialog.getByRole('button', { name: 'Start' }).click();

  const gate = page.locator('.workflow-step', { hasText: 'Gate' });
  await expect(gate).toContainText('needs approval');
  await expect(page.locator('.workflow-step', { hasText: 'Worker' })).toContainText('waiting');
  await gate.getByRole('button', { name: 'Approve' }).click();
  await expect(page.locator('.workflow-step', { hasText: 'Worker' })).toContainText('done');
  await expect(page.locator('.workflow-toolbar .studio-badge')).toHaveText('Completed');
  await expect(page.getByRole('button', { name: 'Run again' })).toBeVisible();
});

test('a rejected approval fails the workflow and shows why', async ({ page, request }) => {
  const name = unique('reject');
  await createApprovalWorkflow(request, name, await createAgent(request, unique('agent')));
  await openView(page, 'Workflows');
  await page.getByRole('button', { name: new RegExp(name) }).click();
  await page.getByRole('button', { name: 'Start', exact: true }).click();
  await page.getByRole('dialog').getByRole('checkbox').check();
  await page.getByRole('dialog').getByRole('button', { name: 'Start' }).click();
  await page.locator('.workflow-step', { hasText: 'Gate' }).getByRole('button', { name: 'Reject' }).click();
  await expect(page.locator('.workflow-stopped')).toContainText('did not approve');
  await expect(page.locator('.workflow-toolbar .studio-badge')).toHaveText('Failed');
});

test('a graph is built in the editor, validated and saved as version 1', async ({ page, request }) => {
  const agentName = unique('agent');
  await createAgent(request, agentName);
  await openView(page, 'Workflows');
  await page.getByRole('button', { name: 'New workflow' }).click();
  await page.getByLabel('Workflow name').fill(unique('built'));
  const save = page.getByRole('button', { name: 'Save new version' });
  await expect(page.locator('.workflow-validation')).toContainText('it needs an outgoing link');
  await expect(save).toBeDisabled();

  await page.getByRole('button', { name: 'Agent step' }).first().click(); // toolbar "+ Agent step"
  await page.getByLabel('Saved agent').selectOption({ label: agentName });
  await expect(page.locator('.workflow-validation')).toContainText('it needs an outgoing link');

  // Input -> Agent by drawing, Agent -> Output through the keyboard-friendly list.
  await page.getByRole('button', { name: 'Input (Input)' }).click();
  await page.getByRole('button', { name: 'Draw a link from here' }).click();
  await page.getByRole('button', { name: /Agent 1 \(Agent step\)/ }).click();
  await page.getByRole('button', { name: 'Output (Output)' }).click();
  await page.getByRole('button', { name: 'Add an incoming link' }).click();
  await page.getByLabel('Previous step').selectOption({ label: 'Agent 1' });
  await page.getByRole('button', { name: 'Connect', exact: true }).click();

  await expect(page.locator('.workflow-validation')).toHaveText('The graph looks valid.');
  await save.click();
  await expect(page.getByRole('status').filter({ hasText: 'Saved as version 1.' })).toBeVisible();
});

test('the output has no outgoing links and a step cannot be linked to itself', async ({ page, request }) => {
  await createAgent(request, unique('agent'));
  await openView(page, 'Workflows');
  await page.getByRole('button', { name: 'New workflow' }).click();
  await page.getByRole('button', { name: 'Output (Output)' }).click();
  await expect(page.getByRole('button', { name: 'Draw a link from here' })).toHaveCount(0); // the output has no outgoing links
  await page.getByRole('button', { name: 'Input (Input)' }).click();
  await page.getByRole('button', { name: 'Draw a link from here' }).click();
  await page.getByRole('button', { name: 'Input (Input)' }).click({ force: true }); // itself: ignored, still linking
  await expect(page.getByText(/Click the step to connect from/)).toBeVisible();
});

test('nodes can be dragged and moved with the keyboard', async ({ page, request }) => {
  await createAgent(request, unique('agent'));
  await openView(page, 'Workflows');
  await page.getByRole('button', { name: 'New workflow' }).click();
  const input = page.getByRole('button', { name: 'Input (Input)' });
  const before = await input.boundingBox();
  expect(before).not.toBeNull();
  await page.mouse.move(before!.x + 40, before!.y + 30);
  await page.mouse.down();
  await page.mouse.move(before!.x + 140, before!.y + 90, { steps: 6 });
  await page.mouse.up();
  const dragged = await input.boundingBox();
  expect(dragged!.x - before!.x).toBeGreaterThan(60);
  expect(dragged!.y - before!.y).toBeGreaterThan(30);
  await input.focus();
  await page.keyboard.press('ArrowRight');
  const nudged = await input.boundingBox();
  expect(nudged!.x).toBeGreaterThan(dragged!.x);
});

test('two versions can be compared and nothing is restored', async ({ page, request }) => {
  const name = unique('versions');
  const id = await createApprovalWorkflow(request, name, await createAgent(request, unique('agent')));
  const current = (await (await request.get('/api/workflows')).json()).definitions.find((d: { id: string }) => d.id === id);
  current.spec.nodes[1].label = 'Gate renamed';
  const second = await request.post('/api/workflows', { data: { id, name, expected_version: 1, spec: current.spec } });
  expect(second.status()).toBe(200);
  await openView(page, 'Workflows');
  await page.getByRole('button', { name: new RegExp(name) }).click();
  await page.getByRole('button', { name: 'Compare versions…' }).click();
  const dialog = page.getByRole('dialog', { name: /Compare versions/ });
  await expect(dialog).toContainText('Changed steps');
  await expect(dialog).toContainText('Gate renamed');
  await expect(dialog).toContainText('nothing is restored or changed');
});
