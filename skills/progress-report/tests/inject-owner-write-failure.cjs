const fs = require('fs');
const real = fs.writeFileSync;
fs.writeFileSync = function (p, ...rest) {
  if (String(p).endsWith('/owner')) { const e = new Error('ENOSPC: no space left on device'); e.code='ENOSPC'; throw e; }
  return real.call(fs, p, ...rest);
};
process.argv = [process.argv[0], process.argv[2], ...process.argv.slice(3)];
require(process.argv[1]);
