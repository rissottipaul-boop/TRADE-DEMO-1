// Пробный запуск Morphy: отдельный каталог состояния и только localhost.
// Это разделение данных, а не песочница для выполнения произвольных AI-команд.
import os from 'node:os';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { syncBuiltinESMExports } from 'node:module';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const trialHome = path.join(project, 'data/morphy-eval/home');
os.homedir = () => trialHome;
syncBuiltinESMExports();

// Morphy 0.5.0 использует listen(port) без адреса. Сужаем привязку всех
// TCP-серверов этого процесса и его Node-потомков до IPv4 loopback.
const originalListen = net.Server.prototype.listen;
net.Server.prototype.listen = function (...args) {
  if (typeof args[0] === 'number') {
    if (typeof args[1] === 'string') args[1] = '127.0.0.1';
    else args.splice(1, 0, '127.0.0.1');
  } else if (args[0] && typeof args[0] === 'object' && 'port' in args[0]) {
    args[0] = { ...args[0], host: '127.0.0.1' };
  }
  return originalListen.apply(this, args);
};
