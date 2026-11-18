# frozen_string_literal: true

require_relative "lib/prodbat/version"

Gem::Specification.new do |spec|
  spec.name = "prodbat"
  spec.version = Prodbat::VERSION
  spec.authors = ["Prodbat"]
  spec.summary = "Monitoring for apps built with AI: server errors, logs, scheduled jobs and Sidekiq queues, reported to Prodbat."
  spec.description = "Reports unhandled exceptions with their cause, other 5xx answers, log lines, job check-ins, " \
                     "Sidekiq queue health and Stripe events to a Prodbat server. Rails and Rack. No dependencies."
  spec.homepage = "https://github.com/prodbat/prodbat"
  spec.license = "MIT"
  spec.required_ruby_version = ">= 3.0"
  spec.metadata = {
    "source_code_uri" => "https://github.com/prodbat/prodbat/tree/main/packages/ruby",
    "rubygems_mfa_required" => "true"
  }
  spec.files = Dir["lib/**/*.rb"] + %w[README.md LICENSE]
  spec.require_paths = ["lib"]
end
