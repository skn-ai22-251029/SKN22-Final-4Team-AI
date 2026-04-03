const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const configDir = process.env.N8N_CONFIG_DIR || '/n8n-config';
const workflowsDir = path.join(configDir, 'workflows');
const manifestPath = path.join(configDir, 'workflow-manifest.json');
const postgresCredentialName = process.env.N8N_POSTGRES_CREDENTIAL_NAME || 'pg-credentials';

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function listWorkflowFiles() {
  return fs
    .readdirSync(workflowsDir)
    .filter((name) => name.endsWith('.json'))
    .sort();
}

function validateManifest() {
  if (!fs.existsSync(manifestPath)) {
    throw new Error(`workflow manifest not found: ${manifestPath}`);
  }

  const manifest = readJson(manifestPath);
  if (!Array.isArray(manifest) || manifest.length === 0) {
    throw new Error('workflow manifest must be a non-empty JSON array');
  }

  const actual = listWorkflowFiles();
  const expected = [...manifest].sort();
  const unexpected = actual.filter((name) => !manifest.includes(name));
  const missing = manifest.filter((name) => !actual.includes(name));

  if (unexpected.length) {
    throw new Error(`unexpected workflow files present: ${unexpected.join(', ')}`);
  }
  if (missing.length) {
    throw new Error(`workflow files missing from disk: ${missing.join(', ')}`);
  }

  const ids = new Map();
  const workflows = [];
  for (const name of manifest) {
    const filePath = path.join(workflowsDir, name);
    const workflow = readJson(filePath);
    const workflowId = String(workflow.id || '').trim();
    if (!workflowId) {
      throw new Error(`workflow file must define id: ${name}`);
    }
    if (ids.has(workflowId)) {
      throw new Error(`duplicate workflow id ${workflowId}: ${ids.get(workflowId)}, ${name}`);
    }
    ids.set(workflowId, name);
    workflows.push({
      name,
      id: workflowId,
      active: workflow.active !== false,
    });
  }

  const wf12Path = 'WF-12_heygen_generate.json';
  if (manifest.includes(wf12Path)) {
    const wf12 = readJson(path.join(workflowsDir, wf12Path));
    if (String(wf12.id || '') !== 'WF12HeygenV2Run') {
      throw new Error('WF-12_heygen_generate.json must keep id=WF12HeygenV2Run');
    }
  }

  return workflows;
}

function sanitizeWorkflow(workflow) {
  const sanitized = { ...workflow };
  // n8n CLI import can fail on name-only tags without tag ids.
  delete sanitized.tags;
  return sanitized;
}

function tryParseJson(text) {
  const trimmed = String(text || '').trim();
  if (!trimmed) {
    return null;
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    // Some CLI builds can print preface lines before JSON.
    const start = Math.min(
      ...['[', '{']
        .map((ch) => trimmed.indexOf(ch))
        .filter((idx) => idx >= 0),
    );
    if (!Number.isFinite(start) || start < 0) {
      return null;
    }
    return JSON.parse(trimmed.slice(start));
  }
}

function loadCredentialsFromCli() {
  const raw = execFileSync('n8n', ['export:credentials', '--all'], {
    encoding: 'utf8',
  });
  const parsed = tryParseJson(raw);
  if (!parsed) {
    throw new Error('failed to parse output of n8n export:credentials --all');
  }
  if (Array.isArray(parsed)) {
    return parsed;
  }
  if (Array.isArray(parsed.credentials)) {
    return parsed.credentials;
  }
  if (Array.isArray(parsed.data)) {
    return parsed.data;
  }
  throw new Error('unsupported credentials export payload format');
}

function resolvePostgresCredential() {
  const credentials = loadCredentialsFromCli();
  const exact = credentials.filter((entry) => {
    if (!entry || typeof entry !== 'object') {
      return false;
    }
    const name = String(entry.name || '');
    const type = String(entry.type || '');
    return name === postgresCredentialName && type === 'postgres';
  });
  if (exact.length === 1) {
    const resolved = exact[0];
    const id = String(resolved.id || '').trim();
    if (!id) {
      throw new Error(`credential "${postgresCredentialName}" has empty id`);
    }
    return { id, name: postgresCredentialName };
  }
  if (exact.length > 1) {
    throw new Error(
      `multiple postgres credentials found for name "${postgresCredentialName}" (${exact
        .map((c) => c.id)
        .join(', ')})`,
    );
  }

  const fallbackByName = credentials.filter((entry) => {
    if (!entry || typeof entry !== 'object') {
      return false;
    }
    return String(entry.name || '') === postgresCredentialName;
  });
  if (fallbackByName.length === 1) {
    const resolved = fallbackByName[0];
    const id = String(resolved.id || '').trim();
    const type = String(resolved.type || '').trim() || 'unknown';
    if (!id) {
      throw new Error(`credential "${postgresCredentialName}" has empty id`);
    }
    console.warn(
      `[workflow-sync] WARNING: credential "${postgresCredentialName}" type is "${type}" (expected "postgres"), continuing`,
    );
    return { id, name: postgresCredentialName };
  }
  if (fallbackByName.length > 1) {
    throw new Error(
      `multiple credentials found for name "${postgresCredentialName}" (${fallbackByName
        .map((c) => `${c.id}:${c.type || 'unknown'}`)
        .join(', ')})`,
    );
  }
  throw new Error(`postgres credential not found: name="${postgresCredentialName}"`);
}

function bindPostgresCredentials(workflow, credentialRef) {
  const bound = { ...workflow };
  const nodes = Array.isArray(bound.nodes) ? bound.nodes.map((node) => ({ ...node })) : [];
  let replaced = 0;

  for (const node of nodes) {
    const creds = node.credentials;
    if (!creds || typeof creds !== 'object' || !creds.postgres) {
      continue;
    }
    node.credentials = {
      ...creds,
      postgres: {
        id: credentialRef.id,
        name: credentialRef.name,
      },
    };
    replaced += 1;
  }

  bound.nodes = nodes;
  return { workflow: bound, replaced };
}

function importManifest(workflows, postgresCredential) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'n8n-import-'));
  try {
    for (const workflowMeta of workflows) {
      const sourcePath = path.join(workflowsDir, workflowMeta.name);
      const tempPath = path.join(tempDir, workflowMeta.name);
      const baseWorkflow = sanitizeWorkflow(readJson(sourcePath));
      const { workflow, replaced } = bindPostgresCredentials(baseWorkflow, postgresCredential);
      if (replaced > 0) {
        console.log(
          `[workflow-sync] mapped postgres credential id=${postgresCredential.id} name=${postgresCredential.name} on ${workflowMeta.name} (${replaced} node(s))`,
        );
      }
      fs.writeFileSync(tempPath, `${JSON.stringify(workflow, null, 2)}\n`, 'utf8');
      console.log(`[workflow-sync] importing ${workflowMeta.name}`);
      execFileSync('n8n', ['import:workflow', '--input', tempPath], { stdio: 'inherit' });
    }
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

function syncActivation(workflows) {
  console.log('[workflow-sync] deactivating all workflows before canonical re-activation');
  execFileSync('n8n', ['update:workflow', '--all', '--active=false'], { stdio: 'inherit' });
  for (const workflowMeta of workflows) {
    if (!workflowMeta.active) {
      continue;
    }
    console.log(`[workflow-sync] activating ${workflowMeta.id} (${workflowMeta.name})`);
    execFileSync('n8n', ['update:workflow', `--id=${workflowMeta.id}`, '--active=true'], {
      stdio: 'inherit',
    });
  }
}

function main() {
  const mode = process.argv[2] || 'check';
  const workflows = validateManifest();
  console.log(`[workflow-sync] manifest OK (${workflows.length} files)`);
  if (mode === 'import') {
    const postgresCredential = resolvePostgresCredential();
    console.log(
      `[workflow-sync] resolved postgres credential: ${postgresCredential.name} (id=${postgresCredential.id})`,
    );
    importManifest(workflows, postgresCredential);
    syncActivation(workflows);
    console.log('[workflow-sync] import complete');
    return;
  }
  if (mode !== 'check') {
    throw new Error(`unsupported mode: ${mode}`);
  }
}

main();
