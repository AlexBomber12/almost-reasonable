import assert from 'node:assert/strict';
import test from 'node:test';
import {CHECK_NAME, resolve, publish} from './trusted-check.mjs';

const repo = 'owner/project';
const fork = 'contributor/project';
const sha = 'a'.repeat(40);
const nextSha = 'b'.repeat(40);

function fixture({forked = false, emptyPayload = false, stale = false} = {}) {
  const source = forked ? fork : repo;
  const sourceId = forked ? 2 : 1;
  const pr = {number: 7, state: 'open', base: {ref: 'main', repo: {id: 1}},
    head: {ref: 'topic', sha: stale ? nextSha : sha,
      repo: {id: sourceId, full_name: source}}};
  const run = {id: 42, run_attempt: 1, repository: {id: 1},
    head_repository: {id: sourceId, full_name: source}, head_sha: sha,
    head_branch: 'topic', event: 'pull_request', workflow_id: 8, name: 'CI',
    path: '.github/workflows/ci.yml', status: 'completed'};
  const event = {repository: {id: 1, full_name: repo}, workflow_run: {
    id: 42, run_attempt: 1, head_sha: sha,
    head_repository: {id: sourceId}, pull_requests: emptyPayload ? [] :
      [{number: 7, head: {sha, repo: {id: sourceId}}}],
  }};
  const checks = [];
  const calls = [];
  const listedPrs = [pr];
  let failMethod;
  const api = async (method, path, body) => {
    calls.push({method, path, body});
    if (failMethod === method && (method === 'POST' || method === 'PATCH'))
      throw new Error('simulated publication error');
    if (path === `/repos/${repo}`) return {id: 1, full_name: repo, default_branch: 'main'};
    if (path.endsWith('/actions/workflows/ci.yml'))
      return {id: 8, name: 'CI', path: '.github/workflows/ci.yml'};
    if (path.endsWith('/actions/runs/42')) return run;
    if (path.includes('/pulls?')) return listedPrs;
    if (path.endsWith('/pulls/7')) return pr;
    if (path.includes('/check-runs?')) return {check_runs: checks};
    if (method === 'POST' && path.endsWith('/check-runs')) {
      const check = {id: 100 + checks.length, ...body};
      checks.push(check);
      return check;
    }
    if (method === 'PATCH' && path.includes('/check-runs/')) {
      Object.assign(checks.find(check => path.endsWith(`/${check.id}`)), body);
      return body;
    }
    throw new Error(`Unexpected API path ${path}`);
  };
  return {api, event, pr, run, checks, calls, listedPrs,
    setFail: method => {failMethod = method;}};
}

function evidence(overrides = {}) {
  return {pr: '7', sha, headRepo: repo, result: 'success', exit: '0', ...overrides};
}

test('resolves same-repository and fork PRs, including empty fork payload', async () => {
  for (const options of [{}, {forked: true, emptyPayload: true}]) {
    const f = fixture(options);
    const target = await resolve(f.api, f.event, repo);
    assert.equal(target.sha, sha);
    assert.equal(target.headRepo, options.forked ? fork : repo);
    assert.equal(target.current, true);
  }
});

test('rejects wrong workflow, repository, ambiguous and inconsistent metadata', async () => {
  const f = fixture();
  f.run.workflow_id = 99;
  await assert.rejects(resolve(f.api, f.event, repo), /source workflow/);
  f.run.workflow_id = 8;
  f.event.repository.full_name = fork;
  await assert.rejects(resolve(f.api, f.event, repo), /repository/);
  f.event.repository.full_name = repo;
  f.event.workflow_run.pull_requests.push({number: 8, head: {sha, repo: {id: 1}}});
  await assert.rejects(resolve(f.api, f.event, repo), /Ambiguous/);
  f.event.workflow_run.pull_requests.pop();
  f.event.workflow_run.pull_requests[0].head.sha = nextSha;
  await assert.rejects(resolve(f.api, f.event, repo), /Inconsistent/);
  const g = fixture({forked: true, emptyPayload: true});
  g.listedPrs.push({...g.pr, number: 8});
  await assert.rejects(resolve(g.api, g.event, repo), /Ambiguous/);
});

test('clean scan publishes success only on exact evaluated SHA', async () => {
  const f = fixture();
  f.run.conclusion = 'failure'; // Triggering CI is only a wake-up event.
  const outcome = await publish(f.api, f.event, repo, evidence());
  assert.deepEqual(outcome, {conclusion: 'success', sha});
  assert.equal(f.checks.length, 1);
  assert.equal(f.checks[0].head_sha, sha);
  assert.equal(f.checks[0].name, CHECK_NAME);
  assert.equal(f.checks[0].external_id, 'sec02:42:1');
  assert.equal(f.calls.filter(call => call.method === 'POST')[0].body.status, 'in_progress');
});

test('fork scan publishes on its exact head SHA in the base repository', async () => {
  const f = fixture({forked: true, emptyPayload: true});
  const outcome = await publish(f.api, f.event, repo, evidence({headRepo: fork}));
  assert.equal(outcome.conclusion, 'success');
  assert.equal(f.checks[0].head_sha, sha);
  assert.ok(f.calls.some(call => call.path.includes(`/repos/${repo}/commits/${sha}/check-runs`)));
});

test('findings, scanner errors, missing evidence, and wrong SHA publish failure', async () => {
  for (const change of [
    {result: 'failure', exit: '1'}, {result: 'failure', exit: '2'},
    {result: 'skipped', exit: ''}, {sha: nextSha}, {headRepo: fork},
  ]) {
    const f = fixture();
    const outcome = await publish(f.api, f.event, repo, evidence(change));
    assert.equal(outcome.conclusion, 'failure');
    assert.equal(f.checks[0].conclusion, 'failure');
  }
});

test('changed head and a head changed during publication cannot get success', async () => {
  const f = fixture({stale: true});
  assert.equal((await publish(f.api, f.event, repo, evidence())).conclusion, 'cancelled');
  const g = fixture();
  const api = async (method, path, body) => {
    const response = await g.api(method, path, body);
    if (method === 'POST') g.pr.head.sha = nextSha;
    return response;
  };
  assert.equal((await publish(api, g.event, repo, evidence())).conclusion, 'cancelled');
  assert.equal(g.checks[0].conclusion, 'cancelled');
});

test('duplicate and out-of-order runs do not upgrade failure or replace newer result', async () => {
  const f = fixture();
  f.checks.push({id: 13, name: CHECK_NAME, external_id: 'sec02:42:1', conclusion: 'failure'});
  assert.equal((await publish(f.api, f.event, repo, evidence())).conclusion, 'failure');
  assert.equal(f.checks.length, 1);
  const g = fixture();
  g.checks.push({id: 14, name: CHECK_NAME, external_id: 'sec02:43:1', conclusion: 'failure'});
  assert.equal((await publish(g.api, g.event, repo, evidence())).conclusion, 'obsolete');
  assert.equal(g.calls.filter(call => ['POST', 'PATCH'].includes(call.method)).length, 0);
});

test('publication API errors never create a successful check', async () => {
  for (const method of ['POST', 'PATCH']) {
    const f = fixture();
    f.setFail(method);
    await assert.rejects(publish(f.api, f.event, repo, evidence()), /publication error/);
    assert.equal(f.checks.some(check => check.conclusion === 'success'), false);
  }
});
