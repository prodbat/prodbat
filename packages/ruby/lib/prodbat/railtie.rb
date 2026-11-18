# frozen_string_literal: true

module Prodbat
  # Rails: the middleware goes just inside ShowExceptions, so it sees an exception
  # with its backtrace before Rails turns it into a 500 page. Failed Active Job
  # jobs are reported too.
  class Railtie < ::Rails::Railtie
    initializer "prodbat.middleware" do |app|
      if defined?(::ActionDispatch::ShowExceptions)
        app.middleware.insert_after ::ActionDispatch::ShowExceptions, Prodbat::Middleware
      else
        app.middleware.use Prodbat::Middleware
      end
    end

    initializer "prodbat.active_job" do
      ActiveSupport.on_load(:active_job) do
        around_perform do |job, block|
          block.call
        rescue Exception => e # rubocop:disable Lint/RescueException -- reported, then raised again unchanged
          Prodbat.capture_exception(e, route: "")
          Prodbat.log("Job #{job.class.name} failed: #{e.class}", level: :error, job_id: job.job_id) rescue nil
          raise
        end
      end
    end
  end
end
