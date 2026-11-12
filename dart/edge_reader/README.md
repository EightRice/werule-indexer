# edge_reader

Reads what the werule indexer publishes ([../../publisher/CONTRACT.md](../../publisher/CONTRACT.md)): `networks.json`, each network's DAOs, and each DAO's proposals, members, projects, debates and activity, from immutable content-addressed files plus live pushes from the Worker. werule and the Autonet app depend on it by git ref:

```yaml
dependencies:
  edge_reader:
    git:
      url: https://github.com/EightRice/werule-indexer.git
      path: dart/edge_reader
      ref: master
```

One-shot reads (no live pushes):

```dart
final client = EdgeClient.filesOnly(dataUrl: 'https://data.autonet.computer');
for (final net in await readNetworks(client)) {
  if ((net.data['jurisdictionFactory'] ?? '') == '') continue;
  for (final dao in await readDaos(client, net.name)) {
    print('${dao.id} ${dao.data?['name']} ${(dao.data?['registry'] as Map?)?['jurisdiction.autonet.substrate']}');
  }
}
```

Live lists: `EdgeClient.connect(dataUrl: ..., liveUrl: 'wss://live.autonet.computer')` and `EdgeList`.

Tests: `flutter test` (the fixture in `test/fixtures/published` is a real publisher run over the publisher e2e's seed data).
