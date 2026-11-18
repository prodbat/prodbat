# frozen_string_literal: true

# Runs only where Rails is installed: the gem inside a real, minimal Rails app.
$LOAD_PATH.unshift File.expand_path("../lib", __dir__)
require "minitest/autorun"

begin
  gem "securerandom", "0.3.1" if Gem::Specification.find_all_by_name("securerandom", "0.3.1").any?
  require "action_controller/railtie"
  RAILS = true
rescue LoadError, Gem::LoadError
  RAILS = false
end

if RAILS
  require "prodbat"

  class ProdbatRailsApp < Rails::Application
    config.eager_load = false
    config.logger = Logger.new(nil)
    config.secret_key_base = "x" * 64
    config.hosts.clear
    routes.append { post "/api/checkout/:id", to: "pay#create" }
  end

  class PayController < ActionController::Base
    def create
      raise KeyError, "price_id"
    end
  end

  ProdbatRailsApp.initialize!
end

class ProdbatRailsTest < Minitest::Test
  def test_rails_gets_the_middleware_and_reports_controller_exceptions_by_route
    skip "Rails is not installed" unless RAILS
    stack = ProdbatRailsApp.middleware.map(&:name)
    assert_equal stack.index("ActionDispatch::ShowExceptions") + 1, stack.index("Prodbat::Middleware")

    sent = []
    Prodbat.stub(:enqueue, ->(_method, path, payload = nil) { sent << payload if path == "/api/errors" }) do
      status, = ProdbatRailsApp.call(Rack::MockRequest.env_for("/api/checkout/42", method: "POST"))
      assert_equal 500, status, "Rails still renders its error page"
    end
    report = sent.first
    assert_equal %w[KeyError POST\ /api/checkout/:id], report.values_at(:kind, :route)
    assert_match(/test_rails\.rb:\d+ in .*create/, report[:where])
  end
end
