export function init(options?: { key?: string; url?: string }): void;
export function flush(timeoutMs?: number): Promise<void>;
export function captureException(err: unknown, options?: { route?: string }): void;
export function captureStatus(status: number, route?: string): void;
export interface Log {
  (message: string, fields?: Record<string, unknown>, level?: 'debug' | 'info' | 'warning' | 'error'): void;
  debug(message: string, fields?: Record<string, unknown>): void;
  info(message: string, fields?: Record<string, unknown>): void;
  warn(message: string, fields?: Record<string, unknown>): void;
  error(message: string, fields?: Record<string, unknown>): void;
}
export const log: Log;
export function ping(name: string, every?: string): void;
export function stripeEvent(event: unknown): void;
export function reportQueue(name: string, snapshot?: { waiting?: number; oldestSeconds?: number; done?: number; failed?: number; maxWaitSeconds?: number; system?: string }): void;
export function watchBullMQ(queues: unknown[], options?: { interval?: number }): () => void;
export function express(): (req: any, res: any, next: any) => void;
export function expressErrorHandler(): (err: any, req: any, res: any, next: any) => void;
export function wrap<T extends (request: Request, ...rest: any[]) => any>(handler: T): T;
export function onRequestError(err: unknown, request?: { method?: string; path?: string }): Promise<void>;
