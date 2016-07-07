#pragma once

#include <louloulibs.h>
#include <memory>

class Poller;

using socket_t = int;

class SocketHandler
{
public:
  explicit SocketHandler(std::shared_ptr<Poller> poller, const socket_t socket):
    poller(poller),
    socket(socket)
  {}
  virtual ~SocketHandler() {}
  SocketHandler& operator=(SocketHandler&&) = default;
  SocketHandler(SocketHandler&&) = default;
  SocketHandler& operator=(const SocketHandler&) = delete;
  SocketHandler(const SocketHandler&) = delete;

  virtual void on_recv() = 0;
  virtual void on_send() = 0;
  virtual void connect() = 0;
  virtual bool is_connected() const = 0;

  socket_t get_socket() const
  { return this->socket; }

protected:
  /**
   * A pointer to the poller that manages us, because we need to communicate
   * with it.
   */
  std::shared_ptr<Poller> poller;
  /**
   * The handled socket.
   */
  socket_t socket;
};

