# frozen_string_literal: true

$LOAD_PATH.unshift File.expand_path("../lib", __dir__)
require "minitest/autorun"
require "socket"
require "rack"
require "rack/mock"
require "prodbat"
require "prodbat/sidekiq"

# A stand-in for the Prodbat server: records every request.
class FakeProdbat
  attr_reader :seen, :port

  def initialize
    @seen = Queue.new
    @server = TCPServer.new("127.0.0.1", 0)
    @port = @server.addr[1]
    @thread = Thread.new { loop { handle(@server.accept) } }
  end

  def handle(socket)
    method, path, = socket.gets.to_s.split
    headers = {}
    while (line = socket.gets) && line != "\r\n"
      name, value = line.split(":", 2)
      headers[name.downcase] = value.to_s.strip
    end
    body = socket.read(headers["content-length"].to_i)
    @seen << { method: method, path: path, auth: headers["authorization"],
               body: body.to_s.empty? ? nil : JSON.parse(body) }
    socket.write("HTTP/1.1 202 Accepted\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
  ensure
    socket&.close
  end

  # Everything received so far, after letting the background thread send.
  def drain
    Prodbat.flush(3)
    out = []
    out << @seen.pop until @seen.empty?
    out
  end
end

SERVER = FakeProdbat.new
Prodbat.init(key: "pbs_test", url: "http://127.0.0.1:#{SERVER.port}/")

class ProdbatTest < Minitest::Test
  def setup
    SERVER.drain
  end

  def sent(path)
    SERVER.drain.select { |r| r[:path].start_with?(path) }
  end

  def raise_in_checkout
    raise KeyError, "price_id"
  end

  def test_an_exception_is_reported_with_the_app_line_it_came_from
    begin
      raise_in_checkout
    rescue KeyError => e
      Prodbat.capture_exception(e, route: "POST /api/checkout")
    end
    report = sent("/api/errors").first
    assert_equal "Bearer pbs_test", report[:auth]
    assert_equal %w[KeyError price_id POST\ /api/checkout], report[:body].values_at("kind", "message", "route")
    assert_match(/test_prodbat\.rb:\d+ in .*raise_in_checkout/, report[:body]["where"])
    assert_match(/raise_in_checkout/, report[:body]["trace"])
  end

  def test_log_lines_pings_and_stripe_events
    Prodbat.log("refund issued", order: 1042)
    Prodbat.log("card declined", level: :warn)
    Prodbat.ping("nightly email", every: "24h")
    Prodbat.stripe_event({ "type" => "invoice.paid", "data" => { "object" => {} } })
    got = SERVER.drain
    lines = got.find { |r| r[:path] == "/api/logs" }[:body]["lines"]
    assert_equal [["refund issued", "info", { "order" => 1042 }], ["card declined", "warn", nil]],
                 lines.map { |l| l.values_at("message", "level", "fields") }
    assert(got.any? { |r| r[:path] == "/api/ping/nightly+email?every=24h" })
    assert_equal "invoice.paid", got.find { |r| r[:path] == "/api/stripe" }[:body]["type"]
  end

  def test_the_middleware_reports_exceptions_and_5xx_answers_and_raises_again
    broken = Prodbat::Middleware.new(->(_env) { raise ArgumentError, "bad cart" })
    assert_raises(ArgumentError) { Rack::MockRequest.new(broken).post("/api/pay") }
    failing = Prodbat::Middleware.new(->(_env) { [503, {}, ["down"]] })
    assert_equal 503, Rack::MockRequest.new(failing).get("/status").status
    fine = Prodbat::Middleware.new(->(_env) { [200, {}, ["ok"]] })
    Rack::MockRequest.new(fine).get("/")
    errors = sent("/api/errors").map { |r| r[:body] }
    assert_equal [["ArgumentError", "POST /api/pay"], ["HTTP 503", "GET /status"]], errors.map { |b| b.values_at("kind", "route") }
  end

  def test_rails_routes_are_reported_as_patterns
    env = { "REQUEST_METHOD" => "PATCH", "PATH_INFO" => "/users/42", "action_dispatch.route_uri_pattern" => "/users/:id(.:format)" }
    assert_equal "PATCH /users/:id", Prodbat.route_of(env)
    assert_equal "GET /", Prodbat.route_of({ "REQUEST_METHOD" => "GET", "PATH_INFO" => "" })
  end

  def test_the_logger_sends_what_the_app_already_logs_at_its_level
    logger = Prodbat::Logger.new(level: :warn)
    logger.info("not this")
    logger.warn("disk nearly full")
    logger.error { "payment failed" }
    lines = sent("/api/logs").flat_map { |r| r[:body]["lines"] }
    assert_equal [%w[disk\ nearly\ full warning], %w[payment\ failed error]], lines.map { |l| l.values_at("message", "level") }
  end

  FakeQueue = Struct.new(:name, :size, :latency)
  FakeApi = Struct.new(:queues, :processed, :failed)

  def test_sidekiq_queues_and_failures_are_reported
    api = FakeApi.new([FakeQueue.new("default", 3, 1.2), FakeQueue.new("mailers", 140, 1320.5)], 100, 4)
    totals = Prodbat::Sidekiq.report(api, nil)
    api.processed, api.failed = 160, 11
    Prodbat::Sidekiq.report(api, totals)
    reports = sent("/api/queues").map { |r| r[:body] }
    assert_equal %w[sidekiq sidekiq], reports.map { |r| r["system"] }
    assert_equal [["default", 3, 1.2], ["mailers", 140, 1320.5]], reports[0]["queues"].map { |q| q.values_at("name", "waiting", "oldest_seconds") }
    all = reports[1]["queues"].find { |q| q["name"] == "all queues" }
    assert_equal [nil, 60, 7], all.values_at("waiting", "done", "failed")
  end

  def test_nothing_is_sent_and_nothing_raises_without_settings
    Prodbat.init(key: nil, url: nil)
    saved = ENV.delete("PRODBAT_KEY")
    Prodbat.capture_exception(RuntimeError.new("x"))
    Prodbat.log("x")
    assert_empty SERVER.drain
  ensure
    ENV["PRODBAT_KEY"] = saved if saved
    Prodbat.init(key: "pbs_test", url: "http://127.0.0.1:#{SERVER.port}/")
  end

  def test_a_forked_child_sends_on_its_own
    Prodbat.ping("before-fork")
    SERVER.drain
    pid = fork do
      Prodbat.ping("from-child")
      Prodbat.flush(3)
      exit!(0)
    end
    Process.wait(pid)
    sleep 0.2
    assert(SERVER.drain.any? { |r| r[:path] == "/api/ping/from-child" })
  end

  def test_runtime_is_reported_once
    # The first report of the process told Prodbat this is Ruby; it is not repeated.
    Prodbat.ping("again")
    assert_empty(sent("/api/runtime"))
  end
end
