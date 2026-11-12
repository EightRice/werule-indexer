// lib/src/directory.dart
//
// One-shot reads for apps that only need to list what exists: the networks (networks.json) and a
// network's DAOs (the net scope's daos list, every page). Apps that show live lists use EdgeList.

import 'codec.dart';
import 'edge_client.dart';

/// A network as published in networks.json: its name and its network file's fields (rpc, chainId,
/// jurisdictionFactory, moduleCatalog, ...)
class EdgeNetwork {
  final String name;
  final Map<String, dynamic> data;

  const EdgeNetwork(this.name, this.data);

  String get daoCollection => 'idaos$name';
}

/// Every published network; empty when nothing is published yet
Future<List<EdgeNetwork>> readNetworks(EdgeClient client) async {
  final file = await client.file('networks.json');
  if (file is! Map) return const [];
  return [
    for (final n in (file['networks'] as List?) ?? const [])
      EdgeNetwork((n as Map)['id'] as String, decodeData(n['data'])),
  ];
}

/// Every DAO document of a network, newest first
Future<List<DocEntry>> readDaos(EdgeClient client, String network) =>
    client.withScope(network, null, (scope) async {
      final list = scope.list('daos', loadAll: true);
      try {
        await list.load();
        return list.docs();
      } finally {
        scope.unlist(list);
      }
    });
