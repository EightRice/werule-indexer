import 'dart:convert';
import 'dart:io';

import 'package:edge_reader/edge_reader.dart';
import 'package:flutter_test/flutter_test.dart';

/// Serves the files a real publisher run wrote (test/fixtures/published: the publisher's store e2e
/// seed, two DAOs on Localhost, one with a project)
class DirFetcher implements EdgeFetcher {
  final String root;
  DirFetcher(this.root);

  @override
  Future<Object?> getJson(String path) async {
    final f = File('$root/$path');
    if (!f.existsSync()) return null;
    return jsonDecode(await f.readAsString());
  }
}

void main() {
  final fetcher = DirFetcher('test/fixtures/published');
  // As EdgeClient.filesOnly, over the fixture: no socket, the pointers are read at once
  EdgeClient client() => EdgeClient(fetcher: fetcher, connector: EdgeClient.noLiveHost, liveUrl: 'ws://no-live-host');

  test('networks.json lists the network with its file fields, without checkpoints', () async {
    final nets = await readNetworks(client());
    expect(nets.map((n) => n.name), ['Localhost']);
    expect(nets.single.daoCollection, 'idaosLocalhost');
    expect(nets.single.data['rpc'], 'http://127.0.0.1:8546');
    expect(nets.single.data.containsKey('lastSyncedBlock'), isFalse);
  });

  test('every DAO of the network, decoded', () async {
    final c = client();
    final daos = await readDaos(c, 'Localhost');
    expect(daos.map((d) => d.id).toSet(),
        {'0x5FbDB2315678afecb367f032d93F642f64180aa3', '0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512'});
    final dao0 = daos.firstWhere((d) => d.id == '0x5FbDB2315678afecb367f032d93F642f64180aa3');
    expect(dao0.data!['name'], 'E2E DAO 0');
    expect(dao0.data!['creationDate'], isA<DateTime>());
    expect((dao0.data!['registry'] as Map)['description'], 'x');
    c.dispose();
  });

  test('a network with nothing published has no DAOs', () async {
    final c = client();
    expect(await readDaos(c, 'Nowhere'), isEmpty);
    c.dispose();
  });
}
