# frozen_string_literal: true

module Prodbat
  # Rack middleware: reports every unhandled exception with its cause, and every
  # other 5xx answer. Rails gets it automatically; elsewhere, `use Prodbat::Middleware`.
  class Middleware
    def initialize(app)
      @app = app
    end

    def call(env)
      status, headers, body = @app.call(env)
      Prodbat.capture_status(status, Prodbat.route_of(env)) if status.to_i >= 500 && !env["prodbat.reported"]
      [status, headers, body]
    rescue Exception => e # rubocop:disable Lint/RescueException -- reported, then raised again unchanged
      env["prodbat.reported"] = true
      Prodbat.capture_exception(e, route: Prodbat.route_of(env)) unless e.is_a?(SignalException) || e.is_a?(SystemExit)
      raise
    end
  end
end
