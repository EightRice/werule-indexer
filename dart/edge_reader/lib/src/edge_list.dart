// lib/src/edge_list.dart

import 'package:flutter/foundation.dart';

import 'codec.dart';
import 'edge_client.dart';

/// A list of one scope in display order, read a file page at a time, kept live by pushes. Same
/// surface as the old PagedQuery: [items], [ready], [loadingMore], [hasMore], [error], [loadMore].
/// The UI page size only sets how many docs a load aims for; file pages are fetched whole.
class EdgeList<T> extends ChangeNotifier {
  final EdgeClient _client;
  final T Function(String id, Map<String, dynamic> data) _parse;
  final String listKey;
  final int pageSize;
  final bool live; // kept for the old API; pushes are merged either way
  late final EdgeScope _scope;
  late final ScopeList _list;

  EdgeList(this._client, String network, String? dao, this.listKey, this._parse,
      {this.pageSize = 20, this.live = true, bool loadAll = false}) {
    _scope = _client.acquire(network, dao);
    _list = _scope.list(listKey, loadAll: loadAll);
    _scope.addListener(_onEvent);
    _start();
  }

  bool _ready = false;
  bool _loadingMore = false;
  bool _disposed = false;
  Object? _error;
  List<T>? _items;

  bool get ready => _ready;
  bool get loadingMore => _loadingMore;
  bool get hasMore => _list.hasMore;
  Object? get error => _error;

  /// Count of the whole list: the latest push's counts for a top-level list, else its index
  int? get totalCount => listKey.contains('/') ? _list.indexCount : (_scope.count(listKey) ?? _list.indexCount);

  /// Everything loaded so far, in display order
  List<T> get items => _items ??= [
        for (final e in _list.docs())
          if (_tryParse(e) case final T v) v,
      ];

  T? _tryParse(DocEntry e) {
    try {
      return _parse(e.id, e.data!);
    } catch (err) {
      debugPrint('[EdgeList] could not parse ${e.path}: $err');
      return null;
    }
  }

  void _changed() {
    _items = null;
    if (!_disposed) notifyListeners();
  }

  Future<void> _start() async {
    try {
      await _list.load(minDocs: pageSize);
    } catch (e) {
      _error = e;
    }
    _ready = true;
    _changed();
  }

  void _onEvent(ScopeEvent e) {
    if (!_ready || _disposed) return;
    if (e.rebased || e.counts || e.keys.any(_list.isChild)) _changed();
  }

  /// Fetches the next older file page(s)
  Future<void> loadMore() async {
    if (_loadingMore || !hasMore) return;
    _loadingMore = true;
    _changed();
    try {
      await _list.loadMore(minDocs: pageSize);
    } catch (e) {
      _error = e;
    }
    _loadingMore = false;
    _changed();
  }

  @override
  void dispose() {
    _disposed = true;
    _scope.removeListener(_onEvent);
    _scope.unlist(_list);
    _client.release(_scope);
    super.dispose();
  }
}
