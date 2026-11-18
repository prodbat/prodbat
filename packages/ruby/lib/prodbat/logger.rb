# frozen_string_literal: true

require "logger"

module Prodbat
  # A Logger that sends what the app already logs. On Rails 7.1+:
  #   Rails.logger.broadcast_to(Prodbat::Logger.new(level: :info))
  # Anywhere else, use it like any Logger.
  class Logger < ::Logger
    LEVELS = { ::Logger::DEBUG => "debug", ::Logger::INFO => "info", ::Logger::WARN => "warning",
               ::Logger::ERROR => "error", ::Logger::FATAL => "error", ::Logger::UNKNOWN => "info" }.freeze

    def initialize(level: :info)
      super(nil, level: level)
    end

    def add(severity, message = nil, progname = nil)
      severity ||= ::Logger::UNKNOWN
      return true if severity < level

      message = yield if message.nil? && block_given?
      message = progname if message.nil?
      text = message.is_a?(Exception) ? "#{message.class}: #{message.message}" : message.to_s
      Prodbat.log(text, level: LEVELS.fetch(severity, "info")) unless text.strip.empty?
      true
    end
  end
end
