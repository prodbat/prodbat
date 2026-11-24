# frozen_string_literal: true

module Prodbat
  # Rack middleware: reports every unhandled exception with its cause (except those
  # Rails answers with a 4xx), and every other 5xx answer. Rails gets it
  # automatically; elsewhere, `use Prodbat::Middleware`.
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
      unless e.is_a?(SignalException) || e.is_a?(SystemExit) || Middleware.client_error?(e)
        Prodbat.capture_exception(e, route: Prodbat.route_of(env))
      end
      raise
    end

    # Rails answers some exceptions with a 404, 403 or 400 (ActiveRecord::RecordNotFound,
    # an unknown route): the site working normally, not reported.
    def self.client_error?(error)
      return false unless defined?(::ActionDispatch::ExceptionWrapper)

      status = ::ActionDispatch::ExceptionWrapper.status_code_for_exception(error.class.name)
      status >= 400 && status < 500
    rescue StandardError
      false
    end
  end
end
