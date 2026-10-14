'use strict';

// Configuration-only preload: leave the original application, test suite,
// installed Testcontainers package, and original Ryuk executable untouched.
const os = require('node:os');

function gatewayFor(interfaces) {
  const candidates = [];
  for (const rows of Object.values(interfaces)) {
    for (const row of rows) {
      if (row.internal || row.family !== 'IPv4') continue;
      const ip = row.address.split('.').map(Number);
      const mask = row.netmask.split('.').map(Number);
      if (ip.length !== 4 || mask.length !== 4 ||
          [...ip, ...mask].some(x => !Number.isInteger(x) || x < 0 || x > 255)) {
        throw new Error('Invalid internal Docker IPv4 interface');
      }
      const network = ip.map((part, index) => part & mask[index]);
      if (network[3] !== 0 || mask[3] !== 0) {
        throw new Error('Unsupported Docker IPAM gateway layout');
      }
      network[3] = 1;
      candidates.push(network.join('.'));
    }
  }
  const unique = [...new Set(candidates)];
  if (unique.length !== 1) throw new Error('Expected one internal Docker bridge');
  return unique[0];
}

function configure(env, interfaces) {
  if (env.REVIEWBENCH_DOCKER_PROFILE !== 'hiero42-original-testcontainers-v1') {
    throw new Error('Docker transport profile binding differs');
  }
  const port = Number(env.REVIEWBENCH_DOCKER_BROKER_PORT);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error('Invalid Docker broker port');
  }
  if (env.TESTCONTAINERS_RYUK_DISABLED === 'true' ||
      env.TESTCONTAINERS_RYUK_PRIVILEGED === 'true') {
    throw new Error('Original unprivileged Ryuk lifecycle is required');
  }
  const host = gatewayFor(interfaces);
  env.DOCKER_HOST = `tcp://${host}:${port}`;
  env.TESTCONTAINERS_HOST_OVERRIDE = host;
  env.TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE = '/var/run/docker.sock';
}

module.exports = {gatewayFor, configure};
if (process.env.REVIEWBENCH_DOCKER_PROFILE) {
  configure(process.env, os.networkInterfaces());
}
