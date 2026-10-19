// Runs only from the default branch's workflow_run checkout. Candidate files
// are data for Gitleaks and are never imported or executed here.
import fs from 'node:fs';

export const CHECK_NAME = 'Trusted secret history scan';
const SHA = /^[0-9a-f]{40}$/;
const EXTERNAL = /^sec02:(\d+):(\d+)$/;

function requireValue(ok, message) {
  if (!ok) throw new Error(message);
}

export async function resolve(api, event, repository, preferredNumber) {
  const trigger = event.workflow_run;
  requireValue(event.repository?.full_name === repository && trigger?.id, 'Invalid workflow_run repository');
  const [repo, workflow, run] = await Promise.all([
    api('GET', `/repos/${repository}`),
    api('GET', `/repos/${repository}/actions/workflows/ci.yml`),
    api('GET', `/repos/${repository}/actions/runs/${trigger.id}`),
  ]);
  requireValue(repo.full_name === repository && repo.default_branch === 'main' &&
    event.repository.id === repo.id &&
    run.repository?.id === repo.id &&
    run.head_repository?.id && run.event === 'pull_request' &&
    run.workflow_id === workflow.id && workflow.path === '.github/workflows/ci.yml' &&
    workflow.name === 'CI' && run.name === 'CI' &&
    run.path === workflow.path && run.status === 'completed' &&
    run.id === trigger.id && run.run_attempt === trigger.run_attempt &&
    run.head_sha === trigger.head_sha && SHA.test(run.head_sha) &&
    trigger.head_repository?.id === run.head_repository.id,
  'Invalid source workflow metadata');

  const payloadPRs = trigger.pull_requests || [];
  requireValue(Array.isArray(payloadPRs), 'Invalid pull request metadata');
  let numbers;
  if (preferredNumber) {
    requireValue(Number.isInteger(preferredNumber) && preferredNumber > 0, 'Invalid PR number');
    numbers = [preferredNumber];
  } else if (payloadPRs.length) {
    numbers = [...new Set(payloadPRs.map(pr => pr.number))];
  } else {
    numbers = [];
    for (let page = 1; page <= 10; page++) {
      const prs = await api('GET', `/repos/${repository}/pulls?state=open&per_page=100&page=${page}`);
      requireValue(Array.isArray(prs), 'Invalid PR list');
      numbers.push(...prs.filter(pr => pr.head?.sha === run.head_sha &&
        pr.head.repo?.id === run.head_repository.id).map(pr => pr.number));
      if (prs.length < 100) break;
      requireValue(page < 10, 'PR list exceeds supported pagination');
    }
    numbers = [...new Set(numbers)];
  }
  requireValue(numbers.length === 1 && Number.isInteger(numbers[0]), 'Ambiguous or missing PR identity');
  const number = numbers[0];
  if (payloadPRs.length) {
    requireValue(payloadPRs.length === 1 && payloadPRs[0].number === number &&
      payloadPRs[0].head?.sha === run.head_sha &&
      payloadPRs[0].head?.repo?.id === run.head_repository.id,
    'Inconsistent PR payload');
  }
  const pr = await api('GET', `/repos/${repository}/pulls/${number}`);
  requireValue(pr.number === number && pr.state === 'open' &&
    pr.base?.ref === 'main' && pr.base.repo?.id === repo.id &&
    pr.head?.repo?.id === run.head_repository.id &&
    pr.head.repo.full_name === run.head_repository.full_name &&
    pr.head.ref === run.head_branch,
  'PR source or base changed');
  return {
    number, sha: run.head_sha, headRepo: run.head_repository.full_name,
    current: pr.head.sha === run.head_sha,
    runId: run.id, attempt: run.run_attempt,
  };
}

function order(externalId) {
  const match = EXTERNAL.exec(externalId || '');
  return match ? [Number(match[1]), Number(match[2])] : null;
}

export async function publish(api, event, repository, evidence) {
  const prNumber = Number(evidence.pr) || undefined;
  const target = await resolve(api, event, repository, prNumber);
  const externalId = `sec02:${target.runId}:${target.attempt}`;
  const self = [target.runId, target.attempt];
  const clean = target.current && evidence.result === 'success' &&
    evidence.exit === '0' && evidence.sha === target.sha &&
    evidence.headRepo === target.headRepo && evidence.pr === String(target.number);
  const conclusion = !target.current ? 'cancelled' : clean ? 'success' : 'failure';
  const checks = [];
  for (let page = 1; page <= 10; page++) {
    const response = await api('GET', `/repos/${repository}/commits/${target.sha}/check-runs?check_name=${encodeURIComponent(CHECK_NAME)}&filter=all&per_page=100&page=${page}`);
    requireValue(Array.isArray(response.check_runs), 'Invalid check-run list');
    checks.push(...response.check_runs.filter(check => check.name === CHECK_NAME));
    if (response.check_runs.length < 100) break;
    requireValue(page < 10, 'Check-run list exceeds supported pagination');
  }
  const newer = checks.some(check => {
    const value = order(check.external_id);
    return value && (value[0] > self[0] || value[0] === self[0] && value[1] > self[1]);
  });
  if (newer) return {conclusion: 'obsolete', sha: target.sha};
  const duplicates = checks.filter(check => check.external_id === externalId);
  requireValue(duplicates.length <= 1, 'Duplicate check runs require inspection');
  const existing = duplicates[0];
  // An earlier failure for this same run cannot be upgraded by a duplicate event.
  const finalConclusion = existing?.conclusion === 'failure' ? 'failure' : conclusion;
  const summary = finalConclusion === 'success' ? 'Trusted Gitleaks history scan passed.' :
    finalConclusion === 'cancelled' ? 'PR head changed before publication; scan is obsolete.' :
    'Trusted scan did not produce a verified clean result. Inspect the scanner job.';
  const body = {name: CHECK_NAME, external_id: externalId,
    status: 'completed', conclusion: finalConclusion,
    output: {title: CHECK_NAME, summary}};
  let checkId;
  if (existing) {
    checkId = existing.id;
    await api('PATCH', `/repos/${repository}/check-runs/${checkId}`, {
      status: 'in_progress', output: {title: CHECK_NAME, summary: 'Publishing trusted scan result.'},
    });
  } else {
    // Create pending first: a failed completion API call must not leave success.
    const pending = await api('POST', `/repos/${repository}/check-runs`, {
      name: CHECK_NAME, head_sha: target.sha, external_id: externalId,
      status: 'in_progress', output: {title: CHECK_NAME, summary: 'Publishing trusted scan result.'},
    });
    requireValue(Number.isInteger(pending.id), 'Check-run creation failed');
    checkId = pending.id;
  }
  // Re-read PR state immediately before setting a successful conclusion.
  if (finalConclusion === 'success') {
    const pr = await api('GET', `/repos/${repository}/pulls/${target.number}`);
    if (pr.state !== 'open' || pr.head?.sha !== target.sha ||
        pr.head.repo?.full_name !== target.headRepo) {
      body.conclusion = 'cancelled';
      body.output.summary = 'PR head changed before publication; scan is obsolete.';
    }
  }
  await api('PATCH', `/repos/${repository}/check-runs/${checkId}`, body);
  return {conclusion: body.conclusion, sha: target.sha};
}

export function githubApi(token) {
  return async (method, path, body) => {
    const response = await fetch(`https://api.github.com${path}`, {
      method,
      headers: {Accept: 'application/vnd.github+json', Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'almost-reasonable-secret-scan'},
      body: body && JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`GitHub API ${method} failed: HTTP ${response.status}`);
    return response.json();
  };
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  try {
    const event = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8'));
    const api = githubApi(process.env.GITHUB_TOKEN);
    const repository = process.env.GITHUB_REPOSITORY;
    if (process.argv[2] === 'resolve') {
      const target = await resolve(api, event, repository);
      requireValue(target.current, 'PR head changed before scanning');
      fs.appendFileSync(process.env.GITHUB_OUTPUT,
        `pr=${target.number}\nsha=${target.sha}\nhead_repo=${target.headRepo}\n`);
      console.log(`Resolved PR #${target.number} at ${target.sha}`);
    } else if (process.argv[2] === 'publish') {
      const outcome = await publish(api, event, repository, {
        pr: process.env.SCAN_PR, sha: process.env.SCAN_SHA,
        headRepo: process.env.SCAN_HEAD_REPO, result: process.env.SCAN_RESULT,
        exit: process.env.SCAN_EXIT,
      });
      console.log(`Trusted check ${outcome.conclusion} for ${outcome.sha}`);
    } else throw new Error('Unknown command');
  } catch {
    // API response bodies, event data, and candidate values are never logged.
    console.error('Trusted check failed; inspect workflow metadata and API availability.');
    process.exitCode = 1;
  }
}
