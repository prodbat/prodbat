# frozen_string_literal: true

require "prodbat"

module Prodbat
  # Sidekiq: queues that back up or stop, and jobs that fail.
  #
  #   # config/initializers/prodbat.rb
  #   require "prodbat/sidekiq"
  #   Prodbat::Sidekiq.watch
  #
  # Run it where Sidekiq's Redis is reachable; the web process is fine. It reports
  # every queue's size and how long its oldest job has waited, once a minute, and
  # Sidekiq's failed count across all queues. In the Sidekiq server it also
  # reports each job's exception.
  module Sidekiq
    module_function

    # Starts a background thread; returns it. `api` is for tests.
    def watch(interval: 60, api: nil)
      install_error_handler
      last = nil
      Thread.new do
        Thread.current.name = "prodbat-sidekiq"
        loop do
          begin
            last = report(api || default_api, last)
          rescue StandardError
            nil # Redis hiccup: try again next time
          end
          sleep interval
        end
      end
    end

    # One report. Returns the totals to compare with next time.
    def report(api, last)
      queues = api.queues.map do |queue|
        { name: queue.name, waiting: queue.size, oldest_seconds: queue.latency.to_f.round(1), done: 0, failed: 0 }
      end
      totals = { processed: api.processed.to_i, failed: api.failed.to_i }
      if last
        # Sidekiq counts finished and failed jobs for all queues together.
        queues << { name: "all queues", waiting: nil, oldest_seconds: nil,
                    done: [totals[:processed] - last[:processed], 0].max, failed: [totals[:failed] - last[:failed], 0].max }
      end
      Prodbat.report_queues("sidekiq", queues) unless queues.empty?
      totals
    end

    def install_error_handler
      return unless defined?(::Sidekiq) && ::Sidekiq.respond_to?(:configure_server)

      ::Sidekiq.configure_server do |config|
        config.error_handlers << proc do |error, context, *_|
          job = context.is_a?(Hash) ? (context[:job] || context["job"] || {}) : {}
          Prodbat.capture_exception(error, route: "")
          Prodbat.log("Sidekiq job #{job['class']} failed: #{error.class}", level: :error, queue: job["queue"])
        end
      end
    end

    # Sidekiq's own API, in the shape `report` reads.
    class DefaultApi
      def queues
        ::Sidekiq::Queue.all
      end

      def processed
        stats.processed
      end

      def failed
        stats.failed
      end

      private

      def stats
        ::Sidekiq::Stats.new
      end
    end

    def default_api
      require "sidekiq/api"
      DefaultApi.new
    end
  end
end
