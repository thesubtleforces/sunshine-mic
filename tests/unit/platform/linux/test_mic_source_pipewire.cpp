/**
 * @file tests/unit/platform/linux/test_mic_source_pipewire.cpp
 * @brief Behavioural tests of the PipeWire virtual microphone against a private PipeWire daemon.
 *
 * The daemon runs in its own runtime directory with its own `default` metadata object and no session manager, so
 * the user's real audio session, devices and default source are never involved. The tests are skipped when the
 * `pipewire`, `pw-metadata` or `pw-cli` programs are not installed.
 */

#ifdef __linux__

  // standard includes
  #include <array>
  #include <chrono>
  #include <csignal>
  #include <cstdio>
  #include <cstdlib>
  #include <filesystem>
  #include <fstream>
  #include <memory>
  #include <optional>
  #include <regex>
  #include <string>
  #include <string_view>
  #include <thread>
  #include <utility>
  #include <vector>

  // system includes
  #include <sys/stat.h>
  #include <sys/wait.h>
  #include <unistd.h>

  // lib includes
  #include <gtest/gtest.h>

  // local includes
  #include "src/platform/common.h"
  #include "src/platform/linux/mic_source.h"

namespace {
  using namespace std::chrono_literals;
  using platf::pw_mic::configured_value_t;

  constexpr const char *kKey = "default.configured.audio.source";

  configured_value_t json_value(std::string_view name) {
    return {"Spa:String:JSON", std::string {R"({"name":")"} + std::string {name} + R"("})"};
  }

  bool program_available(const char *name) {
    return std::system((std::string {"command -v "} + name + " >/dev/null 2>&1").c_str()) == 0;
  }

  /**
   * @brief A private PipeWire daemon with (optionally) a `default` metadata object, run in a temporary directory.
   */
  class isolated_pipewire_t {
  public:
    isolated_pipewire_t() = default;
    isolated_pipewire_t(const isolated_pipewire_t &) = delete;
    isolated_pipewire_t &operator=(const isolated_pipewire_t &) = delete;

    ~isolated_pipewire_t() {
      stop();
    }

    /**
     * @brief Start the daemon and wait for its socket.
     * @param with_default_metadata Export a metadata object named `default`, as the session manager would.
     */
    bool start(bool with_default_metadata) {
      auto tmpl = (std::filesystem::temp_directory_path() / "sunshine-mic-pw.XXXXXX").string();
      if (!mkdtemp(tmpl.data())) {
        return false;
      }
      dir_ = tmpl;
      run_dir_ = dir_ / "run";
      std::filesystem::create_directories(run_dir_);
      ::chmod(run_dir_.c_str(), 0700);
      std::filesystem::create_directories(dir_ / "state");

      const auto fragments = dir_ / "cfg" / "pipewire" / "pipewire.conf.d";
      std::filesystem::create_directories(fragments);
      if (with_default_metadata) {
        std::ofstream {fragments / "10-default-metadata.conf"} << "context.objects = [\n"
                                                                  "    { factory = metadata\n"
                                                                  "      args = { metadata.name = default }\n"
                                                                  "    }\n"
                                                                  "]\n";
      }

      pid_ = fork();
      if (pid_ < 0) {
        return false;
      }
      if (pid_ == 0) {
        setsid();
        setenv("XDG_RUNTIME_DIR", run_dir_.c_str(), 1);
        setenv("PIPEWIRE_RUNTIME_DIR", run_dir_.c_str(), 1);
        setenv("XDG_CONFIG_HOME", (dir_ / "cfg").c_str(), 1);
        setenv("XDG_STATE_HOME", (dir_ / "state").c_str(), 1);
        setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent", 1);
        unsetenv("PIPEWIRE_REMOTE");
        const auto log = (dir_ / "pipewire.log").string();
        if (auto *f = std::freopen(log.c_str(), "w", stdout); f == nullptr) {
          _exit(126);
        }
        if (auto *f = std::freopen(log.c_str(), "a", stderr); f == nullptr) {
          _exit(126);
        }
        execlp("pipewire", "pipewire", static_cast<char *>(nullptr));
        _exit(127);
      }

      for (int i = 0; i < 100; ++i) {
        if (std::filesystem::exists(run_dir_ / "pipewire-0")) {
          return true;
        }
        int status = 0;
        if (waitpid(pid_, &status, WNOHANG) == pid_) {
          pid_ = -1;
          return false;
        }
        std::this_thread::sleep_for(50ms);
      }
      return false;
    }

    const std::filesystem::path &run_dir() const {
      return run_dir_;
    }

    /**
     * @brief Kill the daemon without warning.
     */
    void kill_daemon() {
      if (pid_ > 0) {
        ::kill(pid_, SIGKILL);
        waitpid(pid_, nullptr, 0);
        pid_ = -1;
      }
    }

    std::optional<configured_value_t> get_key() const {
      const auto out = capture("pw-metadata -n default 0 " + std::string {kKey});
      static const std::regex pattern {R"(key:'default\.configured\.audio\.source' value:'(.*)' type:'(.*)')"};
      if (std::smatch match; std::regex_search(out, match, pattern)) {
        return configured_value_t {match[2], match[1]};
      }
      return std::nullopt;
    }

    void set_key(const configured_value_t &value) const {
      capture("pw-metadata -n default 0 " + std::string {kKey} + " '" + value.value + "' " + value.type);
    }

    void delete_key() const {
      capture("pw-metadata -n default -d 0 " + std::string {kKey});
    }

    bool node_exists(std::string_view name) const {
      const auto out = capture("pw-cli ls Node");
      return out.find("node.name = \"" + std::string {name} + "\"") != std::string::npos;
    }

    bool wait_for_node(std::string_view name) const {
      for (int i = 0; i < 40; ++i) {
        if (node_exists(name)) {
          return true;
        }
        std::this_thread::sleep_for(50ms);
      }
      return false;
    }

  private:
    void stop() {
      if (pid_ > 0) {
        ::kill(pid_, SIGTERM);
        for (int i = 0; i < 40; ++i) {
          if (waitpid(pid_, nullptr, WNOHANG) == pid_) {
            pid_ = -1;
            break;
          }
          std::this_thread::sleep_for(50ms);
        }
        if (pid_ > 0) {
          ::kill(pid_, SIGKILL);
          waitpid(pid_, nullptr, 0);
          pid_ = -1;
        }
      }
      if (!dir_.empty()) {
        std::error_code ec;
        std::filesystem::remove_all(dir_, ec);
        dir_.clear();
      }
    }

    /**
     * @brief Run a PipeWire client program against the private daemon and return its output.
     */
    std::string capture(const std::string &command) const {
      const auto full = "env XDG_RUNTIME_DIR='" + run_dir_.string() + "' PIPEWIRE_RUNTIME_DIR='" + run_dir_.string() +
                        "' DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent timeout 10 " + command + " 2>&1";
      std::string out;
      if (FILE *pipe = popen(full.c_str(), "r")) {
        std::array<char, 512> buf {};
        while (fgets(buf.data(), static_cast<int>(buf.size()), pipe)) {
          out += buf.data();
        }
        pclose(pipe);
      }
      return out;
    }

    std::filesystem::path dir_;  ///< Temporary directory holding everything of the daemon.
    std::filesystem::path run_dir_;  ///< Private runtime directory (the daemon's socket lives here).
    pid_t pid_ = -1;  ///< Daemon process.
  };

  /**
   * @brief Points this process's PipeWire client library at the private daemon for the duration of a test.
   */
  class MicSinkPipeWireTest: public ::testing::Test {
  protected:
    void SetUp() override {
      if (!program_available("pipewire") || !program_available("pw-metadata") || !program_available("pw-cli")) {
        GTEST_SKIP() << "pipewire, pw-metadata and pw-cli are required";
      }
    }

    void TearDown() override {
      sink_.reset();
      restore_environment();
    }

    /**
     * @brief Start the private daemon and redirect this process to it.
     */
    void start_daemon(bool with_default_metadata = true) {
      ASSERT_TRUE(daemon_.start(with_default_metadata)) << "the private PipeWire daemon did not start";

      save_environment("XDG_RUNTIME_DIR");
      save_environment("PIPEWIRE_RUNTIME_DIR");
      save_environment("PIPEWIRE_REMOTE");
      setenv("XDG_RUNTIME_DIR", daemon_.run_dir().c_str(), 1);
      setenv("PIPEWIRE_RUNTIME_DIR", daemon_.run_dir().c_str(), 1);
      unsetenv("PIPEWIRE_REMOTE");

      // Safety: a test must never be able to reach the user's real daemon.
      ASSERT_EQ(std::string {getenv("PIPEWIRE_RUNTIME_DIR")}, daemon_.run_dir().string());
      ASSERT_NE(daemon_.run_dir().string(), "/run/user/" + std::to_string(getuid()));
    }

    std::unique_ptr<platf::mic_sink_t> make_sink(bool set_default_source = true) {
      return platf::mic_sink_create({platf::pw_mic::kSampleRate, platf::pw_mic::kChannels, set_default_source});
    }

    isolated_pipewire_t daemon_;  ///< The private daemon.
    std::unique_ptr<platf::mic_sink_t> sink_;  ///< Sink created by the test, destroyed before the daemon.

  private:
    void save_environment(const char *name) {
      saved_.emplace_back(name, getenv(name) ? std::optional<std::string> {getenv(name)} : std::nullopt);
    }

    void restore_environment() {
      for (const auto &[name, value] : saved_) {
        if (value) {
          setenv(name.c_str(), value->c_str(), 1);
        } else {
          unsetenv(name.c_str());
        }
      }
      saved_.clear();
    }

    std::vector<std::pair<std::string, std::optional<std::string>>> saved_;  ///< Environment to restore.
  };
}  // namespace

TEST_F(MicSinkPipeWireTest, SwitchesTheDefaultAndRestoresThePreviousSource) {
  start_daemon();
  const auto previous = json_value("alsa_input.user-choice");
  daemon_.set_key(previous);

  sink_ = make_sink();
  ASSERT_TRUE(sink_);
  EXPECT_TRUE(daemon_.wait_for_node("moonlight-mic"));
  EXPECT_EQ(daemon_.get_key(), json_value("moonlight-mic"));

  sink_.reset();
  EXPECT_EQ(daemon_.get_key(), previous);
  EXPECT_FALSE(daemon_.node_exists("moonlight-mic"));
}

TEST_F(MicSinkPipeWireTest, DeletesTheKeyWhenNoSourceWasConfigured) {
  start_daemon();
  ASSERT_FALSE(daemon_.get_key().has_value());

  sink_ = make_sink();
  ASSERT_TRUE(sink_);
  EXPECT_EQ(daemon_.get_key(), json_value("moonlight-mic"));

  sink_.reset();
  EXPECT_FALSE(daemon_.get_key().has_value());
}

TEST_F(MicSinkPipeWireTest, KeepsASourceTheUserChoseDuringTheSession) {
  start_daemon();
  daemon_.set_key(json_value("alsa_input.user-choice"));

  sink_ = make_sink();
  ASSERT_TRUE(sink_);
  ASSERT_EQ(daemon_.get_key(), json_value("moonlight-mic"));

  daemon_.set_key(json_value("bluez_input.airpods"));  // the user picks another microphone mid-session

  sink_.reset();
  EXPECT_EQ(daemon_.get_key(), json_value("bluez_input.airpods"));
}

TEST_F(MicSinkPipeWireTest, KeepsAnExplicitClearingByTheUser) {
  start_daemon();
  daemon_.set_key(json_value("alsa_input.user-choice"));

  sink_ = make_sink();
  ASSERT_TRUE(sink_);
  ASSERT_EQ(daemon_.get_key(), json_value("moonlight-mic"));

  daemon_.delete_key();  // the user clears the configured default mid-session

  sink_.reset();
  EXPECT_FALSE(daemon_.get_key().has_value());
}

TEST_F(MicSinkPipeWireTest, IgnoresAStaleOwnNodeLeftByAKilledSession) {
  start_daemon();
  daemon_.set_key(json_value("moonlight-mic"));  // a previous Sunshine was killed while it owned the default

  sink_ = make_sink();
  ASSERT_TRUE(sink_);

  sink_.reset();
  EXPECT_FALSE(daemon_.get_key().has_value());  // not "restored" to our own dead node
}

TEST_F(MicSinkPipeWireTest, LeavesTheDefaultAloneWhenSwitchingIsDisabled) {
  start_daemon();
  const auto previous = json_value("alsa_input.user-choice");
  daemon_.set_key(previous);

  sink_ = make_sink(false);
  ASSERT_TRUE(sink_);
  EXPECT_TRUE(daemon_.wait_for_node("moonlight-mic"));
  EXPECT_EQ(daemon_.get_key(), previous);

  sink_.reset();
  EXPECT_EQ(daemon_.get_key(), previous);
}

TEST_F(MicSinkPipeWireTest, WorksWithoutADefaultMetadataObject) {
  start_daemon(false);  // no session manager exported a `default` metadata object

  sink_ = make_sink();
  ASSERT_TRUE(sink_);
  EXPECT_TRUE(daemon_.wait_for_node("moonlight-mic"));
  EXPECT_FALSE(daemon_.get_key().has_value());

  sink_.reset();
  EXPECT_FALSE(daemon_.node_exists("moonlight-mic"));
}

TEST_F(MicSinkPipeWireTest, TeardownIsBoundedWhenTheDaemonIsGone) {
  start_daemon();
  daemon_.set_key(json_value("alsa_input.user-choice"));

  sink_ = make_sink();
  ASSERT_TRUE(sink_);
  ASSERT_EQ(daemon_.get_key(), json_value("moonlight-mic"));

  daemon_.kill_daemon();  // PipeWire dies mid-session: the restore cannot be written

  const auto started = std::chrono::steady_clock::now();
  sink_.reset();  // must neither crash nor hang
  EXPECT_LT(std::chrono::steady_clock::now() - started, 8s);
}

#endif  // __linux__
