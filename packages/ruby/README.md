# prodbat (Ruby)

Monitoring for apps built with AI. This gem sends a Ruby app's server errors, log lines, scheduled-job check-ins, Sidekiq queue health and Stripe events to [Prodbat](https://github.com/prodbat/prodbat). It has no dependencies, never raises, and never holds a request up: reports go out from a background thread and are dropped if Prodbat can't be reached.

## Install

```ruby
# Gemfile
gem "prodbat"
```

Set `PRODBAT_KEY` and `PRODBAT_URL` in the environment (`npx prodbat init` writes them to `.env`).

- **Rails**: nothing more. The gem adds its middleware just inside `ShowExceptions`, so every exception is reported with its backtrace and route (`POST /api/checkout/:id`), and failed Active Job jobs are reported too.
- **Sinatra, Hanami, Roda, any Rack app**: `use Prodbat::Middleware` in `config.ru`, before `run`.
- **Sidekiq**: in an initializer, `require "prodbat/sidekiq"` and `Prodbat::Sidekiq.watch`. Every minute it reports each queue's size and how long its oldest job has waited, plus Sidekiq's failed count; in the Sidekiq server it also reports each job's exception.

## Also

```ruby
Prodbat.log("refund issued", order: 1042)          # a log line, with fields
Rails.logger.broadcast_to(Prodbat::Logger.new)      # Rails 7.1+: send what the app already logs
Prodbat.ping("nightly-email", every: "24h")         # at the end of a scheduled job, after it succeeded
Prodbat.stripe_event(event)                          # in the Stripe webhook, after the signature check
Prodbat.capture_exception(error, route: "POST /x")  # report one yourself
```

## Tests

```sh
for f in test/test_*.rb; do MT_NO_PLUGINS=1 ruby -Ilib "$f"; done   # each file in its own process
```

MIT licence.
