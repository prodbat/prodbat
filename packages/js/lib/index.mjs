// The ES module entry: the same library as index.cjs, for `import` in Node,
// Bun, Deno (npm:prodbat), Supabase Edge Functions and bundlers.
import prodbat from './index.cjs';

export const {
  init, flush, captureException, captureStatus, log, ping, stripeEvent, reportQueue, watchBullMQ, bullmqSnapshot,
  express, expressErrorHandler, wrap, onRequestError,
} = prodbat;
export default prodbat;
