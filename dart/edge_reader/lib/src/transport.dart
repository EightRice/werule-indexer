// lib/src/transport.dart

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';

/// A non-404 HTTP failure
class EdgeHttpException implements Exception {
  final String path;
  final int status;
  EdgeHttpException(this.path, this.status);
  @override
  String toString() => 'EdgeHttpException($path: $status)';
}

/// Reads JSON files from the data host
abstract class EdgeFetcher {
  /// The decoded file at [path] (relative to the data base URL), or null on 404
  Future<Object?> getJson(String path);
}

/// Simple GETs: no custom headers, no query strings (CONTRACT.md 9.1)
class HttpEdgeFetcher implements EdgeFetcher {
  final String baseUrl;
  final http.Client _client;
  final Duration timeout;

  HttpEdgeFetcher(String baseUrl, {http.Client? client, this.timeout = const Duration(seconds: 20)})
      : baseUrl = baseUrl.endsWith('/') ? baseUrl.substring(0, baseUrl.length - 1) : baseUrl,
        _client = client ?? http.Client();

  @override
  Future<Object?> getJson(String path) async {
    // A request that hangs would otherwise block polling (one pointer read in flight at a time)
    // and every reader of a cached object
    final res = await _client.get(Uri.parse('$baseUrl/$path')).timeout(timeout);
    if (res.statusCode == 404) return null;
    if (res.statusCode != 200) throw EdgeHttpException(path, res.statusCode);
    return jsonDecode(utf8.decode(res.bodyBytes));
  }
}

/// One open websocket
abstract class EdgeSocket {
  /// Text frames from the server; done when the socket closes, errors when it fails
  Stream<String> get messages;
  void send(String text);
  Future<void> close();
}

/// Opens a socket; completes once it is connected, throws if it cannot connect
typedef EdgeSocketConnector = Future<EdgeSocket> Function(Uri uri);

class _ChannelSocket implements EdgeSocket {
  final WebSocketChannel _channel;
  _ChannelSocket(this._channel);

  @override
  Stream<String> get messages => _channel.stream.map((m) => m is String ? m : utf8.decode(m as List<int>));

  @override
  void send(String text) => _channel.sink.add(text);

  @override
  Future<void> close() async {
    await _channel.sink.close();
  }
}

Future<EdgeSocket> connectWebSocket(Uri uri) async {
  final channel = WebSocketChannel.connect(uri);
  await channel.ready;
  return _ChannelSocket(channel);
}
