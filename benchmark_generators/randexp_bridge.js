#!/usr/bin/env node

'use strict';

const RandExp = require('randexp');

function decodePattern(encoded) {
  return Buffer.from(encoded, 'base64').toString('utf8');
}

function seededRandom(seed) {
  let state = seed >>> 0;
  return function random() {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function validate(pattern) {
  try {
    new RegExp(pattern);
    process.stdout.write(JSON.stringify({
      javascript_regex_valid: true,
      javascript_regex_error: null,
    }) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({
      javascript_regex_valid: false,
      javascript_regex_error: `${error.name}: ${error.message}`,
    }) + '\n');
  }
}

function generate(pattern, count, seed) {
  const generator = new RandExp(pattern);
  const random = seededRandom(seed);
  generator.randInt = (from, to) => from + Math.floor(random() * (to - from + 1));

  const seen = new Set();
  const maxAttempts = Math.max(100, count * 100);
  for (let attempts = 0; attempts < maxAttempts && seen.size < count; attempts += 1) {
    seen.add(generator.gen());
  }

  for (const sample of seen) {
    process.stdout.write(Buffer.from(sample, 'utf8').toString('base64') + '\n');
  }
}

function main() {
  const [mode, encodedPattern, countText = '0', seedText = '0'] = process.argv.slice(2);
  const pattern = decodePattern(encodedPattern);
  if (mode === 'validate') {
    validate(pattern);
    return;
  }
  if (mode !== 'generate') {
    throw new Error(`Unknown mode: ${mode}`);
  }
  generate(pattern, Number.parseInt(countText, 10), Number.parseInt(seedText, 10));
}

try {
  main();
} catch (error) {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
}
