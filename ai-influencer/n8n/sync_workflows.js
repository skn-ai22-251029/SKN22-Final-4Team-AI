const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const configDir = process.env.N8N_CONFIG_DIR || '/n8n-config';
const workflowsDir = path.join(configDir, 'workflows');
const manifestPath = path.join(configDir, 'workflow-manifest.json');

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

function importManifest(workflows) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'n8n-import-'));
  try {
    for (const workflowMeta of workflows) {
      const sourcePath = path.join(workflowsDir, workflowMeta.name);
      const tempPath = path.join(tempDir, workflowMeta.name);
      const workflow = sanitizeWorkflow(readJson(sourcePath));
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
    importManifest(workflows);
    syncActivation(workflows);
    console.log('[workflow-sync] import complete');
    return;
  }
  if (mode !== 'check') {
    throw new Error(`unsupported mode: ${mode}`);
  }
}

main();
