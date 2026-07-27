const readline = require('readline');

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: false
});

let config = {};

let messageCounter = 0;

function sendResponse(id, result) {
  process.stdout.write(JSON.stringify({ id, result }) + '\n');
}

function sendError(id, message) {
  process.stdout.write(JSON.stringify({ id, error: { code: -1, message } }) + '\n');
}

function handleRequest(request) {
  const { id, method, params } = request;

  if (!id || !method) {
    process.stderr.write('Missing id or method in request\n');
    return;
  }

  try {
    switch (method) {
      case 'initialize':
        handleInitialize(id, params);
        break;
      case 'configure':
        handleConfigure(id, params);
        break;
      case 'deliver':
        handleDeliver(id, params);
        break;
      case 'edit_message':
        handleEditMessage(id, params);
        break;
      case 'delete_message':
        handleDeleteMessage(id, params);
        break;
      case 'react':
        handleReact(id, params);
        break;
      default:
        sendError(id, `Unknown method: ${method}`);
    }
  } catch (err) {
    sendError(id, err.message);
  }
}

function handleInitialize(id, params) {
  config = Object.assign(config, params || {});
  sendResponse(id, {
    name: 'test-js',
    capabilities: {
      inbound: true,
      outbound: true
    }
  });
}

function handleConfigure(id, params) {
  config = Object.assign(config, params || {});
  sendResponse(id, {
    configured: true
  });
}

function handleDeliver(id, params) {
  messageCounter++;
  sendResponse(id, {
    delivered: true,
    external_id: 'test-js-' + messageCounter
  });
}

function handleEditMessage(id, params) {
  sendResponse(id, {
    delivered: true,
    external_id: 'test-js-edited'
  });
}

function handleDeleteMessage(id, params) {
  sendResponse(id, {
    delivered: true,
    external_id: 'test-js-deleted'
  });
}

function handleReact(id, params) {
  sendResponse(id, {
    reacted: true
  });
}

rl.on('line', (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;

  try {
    const request = JSON.parse(trimmed);
    if (request.id && request.method) {
      handleRequest(request);
    } else {
      process.stderr.write('Invalid JSON-RPC message (missing id or method): ' + trimmed + '\n');
    }
  } catch (err) {
    process.stderr.write('Failed to parse JSON: ' + trimmed + '\n');
  }
});

rl.on('close', () => {
  process.exit(0);
});
