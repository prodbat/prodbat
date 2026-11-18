# frozen_string_literal: true

# Prodbat for Ruby servers: errors with their cause, log lines, scheduled jobs,
# Sidekiq queues and Stripe events. No dependencies beyond the standard library.
#
#   # Gemfile: gem "prodbat"
#   # Rails: nothing more. The middleware and Active Job reporting are added by the gem.
#   # Rack (Sinatra, Hanami, Roda): use Prodbat::Middleware   (in config.ru)
#   # Sidekiq: Prodbat::Sidekiq.watch                        (in an initializer)
#
#   Prodbat.log("refund issued", order: 1042)
#   Prodbat.ping("nightly-email", every: "24h")   # at the end of a scheduled job
#   Prodbat.stripe_event(event)                    # in the Stripe webhook, after the signature check
#
# Set PRODBAT_KEY and PRODBAT_URL in the environment. Nothing here ever raises or
# holds a request up: reports go out from a background thread and are dropped if
# Prodbat cannot be reached.

require "json"
require "net/http"
require "rbconfig"
require "uri"
require "prodbat/version"

module Prodbat
  MAX_QUEUE = 200
  MAX_LOGS = 2000
  LIBRARY_DIRS = ([RbConfig::CONFIG["rubylibdir"], RbConfig::CONFIG["sitelibdir"]] +
                  (defined?(Gem) ? Gem.path : [])).compact.uniq.freeze

  @config = { key: nil, url: nil }
  @lock = Mutex.new
  @queue = []
  @logs = []
  @thread = nil
  @pid = nil
  @busy = false
  @runtime_sent = false

  class << self
    # Optional. Without it, PRODBAT_KEY and PRODBAT_URL are read from the environment.
    def init(key: nil, url: nil)
      @config = { key: key, url: url }
    end

    def settings
      key = @config[:key] || ENV["PRODBAT_KEY"]
      url = (@config[:url] || ENV["PRODBAT_URL"] || "").sub(%r{/+\z}, "")
      [key, url.empty? ? nil : url]
    end

    # ---- sending ---------------------------------------------------------------

    def deliver(method, path, payload)
      key, url = settings
      return unless key && url

      uri = URI.parse(url + path)
      http = Net::HTTP.new(uri.host, uri.port)
      http.use_ssl = uri.scheme == "https"
      http.open_timeout = http.read_timeout = http.write_timeout = 5
      request = Net::HTTPGenericRequest.new(method, !payload.nil?, true, uri.request_uri)
      request["Authorization"] = "Bearer #{key}"
      request["Content-Type"] = "application/json"
      request["User-Agent"] = "prodbat-ruby/#{VERSION}"
      request.body = JSON.generate(payload) unless payload.nil?
      http.request(request)
    rescue StandardError
      nil # reporting must never become the problem
    end

    def enqueue(method, path, payload = nil)
      start
      @lock.synchronize { @queue << [method, path, payload] if @queue.size < MAX_QUEUE }
    rescue StandardError
      nil
    end

    # Wait for everything queued to go out. Called when the process exits.
    def flush(timeout = 2.0)
      deadline = monotonic + timeout
      sleep 0.02 while (pending? || @busy) && monotonic < deadline
    end

    # ---- errors ------------------------------------------------------------------

    # Report an exception, with the deepest line of the app's own code it passed through.
    def capture_exception(error, route: "")
      trace = (["#{error.class}: #{error.message}"] + (error.backtrace || [])).join("\n")
      enqueue("POST", "/api/errors", {
        kind: error.class.name.to_s[0, 200],
        message: error.message.to_s[0, 1000],
        route: route.to_s[0, 300],
        where: where(error)[0, 300],
        trace: trace.length > 20_000 ? trace[-20_000..] : trace,
        status: 500
      })
    rescue StandardError
      nil
    end

    # Report a 5xx answer that did not come from an exception.
    def capture_status(status, route = "")
      return unless status.to_i >= 500

      enqueue("POST", "/api/errors", { kind: "HTTP #{status.to_i}", status: status.to_i, route: route.to_s[0, 300] })
    end

    # "app/controllers/checkouts_controller.rb:88 in create"
    def where(error)
      frames = error.backtrace_locations || []
      own = frames.reject { |f| f.path.nil? || LIBRARY_DIRS.any? { |dir| f.path.start_with?(dir) } || f.path.include?("/gems/") }
      frame = own.first || frames.first
      return "" unless frame

      "#{frame.path.split('/').last(2).join('/')}:#{frame.lineno} in #{frame.label}"
    end

    # ---- logs --------------------------------------------------------------------

    # Prodbat.log("refund issued", order: 1042)  ·  Prodbat.log("card declined", level: :warn)
    def log(message, level: :info, **fields)
      @lock.synchronize do
        if @logs.size < MAX_LOGS
          @logs << { message: message.to_s[0, 4000], level: level.to_s.downcase, t: Time.now.to_f,
                     fields: fields.empty? ? nil : fields }
        end
      end
      start
    rescue StandardError
      nil
    end

    # ---- jobs and payments -----------------------------------------------------------

    # A scheduled job checks in, at the end, after the work has succeeded. every: "15m", "24h", "7d".
    def ping(name, every: nil)
      path = "/api/ping/#{URI.encode_www_form_component(name.to_s)}"
      enqueue("POST", every ? "#{path}?every=#{URI.encode_www_form_component(every.to_s)}" : path)
    end

    # Forward a Stripe webhook event, after its signature has been checked.
    def stripe_event(event)
      payload = if event.is_a?(Hash) then event
                elsif event.respond_to?(:to_hash) then event.to_hash
                else JSON.parse(event.to_s)
                end
      enqueue("POST", "/api/stripe", payload)
    rescue StandardError
      nil
    end

    # One snapshot of some queues: [{ name:, waiting:, oldest_seconds:, done:, failed: }].
    def report_queues(system, snapshots)
      enqueue("POST", "/api/queues", { system: system.to_s, queues: snapshots })
    end

    # "POST /users/:id" on Rails 7.1+, otherwise the path.
    def route_of(env)
      pattern = env["action_dispatch.route_uri_pattern"].to_s.sub("(.:format)", "")
      path = pattern.empty? ? env["PATH_INFO"].to_s : pattern
      "#{env['REQUEST_METHOD']} #{path.empty? ? '/' : path}"
    end

    private

    def monotonic
      Process.clock_gettime(Process::CLOCK_MONOTONIC)
    end

    def pending?
      @lock.synchronize { !@queue.empty? || !@logs.empty? }
    end

    # One worker thread per process. After a fork (Puma, Unicorn, Sidekiq) the
    # child starts its own and does not inherit the parent's queue.
    def start
      first = false
      @lock.synchronize do
        if @pid != Process.pid
          @queue = []
          @logs = [] unless @pid.nil?
          @thread = nil
          first = @pid.nil? && !@runtime_sent
          @runtime_sent = true
          @pid = Process.pid
        end
        @thread = Thread.new { work } unless @thread&.alive?
      end
      @lock.synchronize { @queue.unshift(["PUT", "/api/runtime", { language: "ruby", version: RUBY_VERSION }]) } if first
    end

    def work
      Thread.current.name = "prodbat"
      last_logs = monotonic
      loop do
        item = @lock.synchronize { @queue.shift }
        @busy = true
        if item
          deliver(*item)
        elsif !@logs.empty? && (monotonic - last_logs >= 1.0 || @logs.size >= 500)
          batch = @lock.synchronize { @logs.shift(500) }
          deliver("POST", "/api/logs", { lines: batch })
          last_logs = monotonic
        end
        @busy = false
        sleep 0.05 unless item
      end
    rescue StandardError
      @busy = false
      retry
    end
  end
end

at_exit { Prodbat.flush }

require "prodbat/middleware"
require "prodbat/logger"
require "prodbat/railtie" if defined?(Rails::Railtie)
