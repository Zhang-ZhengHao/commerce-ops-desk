import { randomUUID } from 'node:crypto';

import { expect, test } from './fixtures';

interface SessionPayload {
  workspace: { id: string; name: string; expires_at: string };
  identity: {
    user_id: string;
    membership_id: string;
    display_name: string;
    role: 'manager' | 'agent';
  };
  available_roles: Array<'manager' | 'agent'>;
  csrf_token: string;
}

function isApiCall(response: { url(): string; request(): { method(): string } }, path: string) {
  return (
    new URL(response.url()).pathname === path && response.request().method() === 'POST'
  );
}

test.describe('I02 demo identity', () => {
  test('creates a Manager session, restores it, and securely rotates to Agent', async ({
    context,
    page,
    request,
  }) => {
    await page.goto('/');

    const createResponsePromise = page.waitForResponse((response) =>
      isApiCall(response, '/api/demo/workspaces'),
    );
    await page.getByRole('button', { name: 'Enter as Manager' }).click();
    const createResponse = await createResponsePromise;
    expect(createResponse.status()).toBe(201);
    const created = (await createResponse.json()) as SessionPayload;
    expect(created.identity.role).toBe('manager');
    expect(created.identity.display_name).toBe('Demo Manager');

    await expect(
      page.getByRole('heading', { level: 1, name: 'Manager workspace' }),
    ).toBeVisible();
    await expect(page.getByText(created.workspace.name)).toBeVisible();
    await expect(page.getByText('Demo Manager')).toBeVisible();
    await expect(page.getByText('Manager access')).toBeVisible();

    const initialCookie = (await context.cookies()).find(
      (cookie) => cookie.name === 'commerce_ops_session',
    );
    expect(initialCookie).toMatchObject({
      httpOnly: true,
      path: '/',
      sameSite: 'Strict',
    });
    expect(initialCookie?.value).toBeTruthy();

    const restoreResponsePromise = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === '/api/session' &&
        response.request().method() === 'GET',
    );
    await page.reload();
    const restoreResponse = await restoreResponsePromise;
    expect(restoreResponse.status()).toBe(200);
    const restored = (await restoreResponse.json()) as SessionPayload;
    expect(restored.workspace.id).toBe(created.workspace.id);
    expect(restored.identity.membership_id).toBe(created.identity.membership_id);
    await expect(page.getByText('Demo Manager')).toBeVisible();

    const switchResponsePromise = page.waitForResponse((response) =>
      isApiCall(response, '/api/demo/role'),
    );
    await page.getByRole('button', { name: 'Switch to Agent' }).click();
    const switchResponse = await switchResponsePromise;
    expect(switchResponse.status()).toBe(200);
    const switched = (await switchResponse.json()) as SessionPayload;
    expect(switched.identity.role).toBe('agent');
    expect(switched.csrf_token).not.toBe(restored.csrf_token);
    await expect(page.getByText('Demo Agent')).toBeVisible();
    await expect(
      page.getByRole('heading', { level: 1, name: 'Agent workspace' }),
    ).toBeVisible();
    await expect(page.getByText('Agent access')).toBeVisible();

    const rotatedCookie = (await context.cookies()).find(
      (cookie) => cookie.name === 'commerce_ops_session',
    );
    expect(rotatedCookie?.value).toBeTruthy();
    expect(rotatedCookie?.value).not.toBe(initialCookie?.value);

    const origin = new URL(page.url()).origin;
    const oldSessionResponse = await request.get('/api/session', {
      headers: { Cookie: `commerce_ops_session=${initialCookie?.value ?? ''}` },
    });
    expect(oldSessionResponse.status()).toBe(401);

    const oldCsrfResponse = await request.post('/api/demo/role', {
      data: { role: 'manager' },
      headers: {
        Cookie: `commerce_ops_session=${rotatedCookie?.value ?? ''}`,
        'Idempotency-Key': randomUUID(),
        Origin: origin,
        'X-CSRF-Token': restored.csrf_token,
      },
    });
    expect(oldCsrfResponse.status()).toBe(403);
  });

  test('creates an Agent identity directly without exposing Manager first', async ({
    page,
  }) => {
    await page.goto('/');

    const createResponsePromise = page.waitForResponse((response) =>
      isApiCall(response, '/api/demo/workspaces'),
    );
    await page.getByRole('button', { name: 'Enter as Agent' }).click();
    const createResponse = await createResponsePromise;
    expect(createResponse.status()).toBe(201);
    const created = (await createResponse.json()) as SessionPayload;

    expect(created.identity.role).toBe('agent');
    expect(created.identity.display_name).toBe('Demo Agent');
    await expect(page.getByText('Demo Agent')).toBeVisible();
    await expect(
      page.getByRole('heading', { level: 1, name: 'Agent workspace' }),
    ).toBeVisible();
    await expect(page.getByText('Agent access')).toBeVisible();
    await expect(page.getByText('Demo Manager')).toHaveCount(0);
  });
});
