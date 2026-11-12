/// Reads the files the werule indexer publishes (publisher/CONTRACT.md): networks.json, per network
/// and per DAO a pointer to an immutable head, content-addressed pages, and live pushes from the
/// Worker. Used by werule and the Autonet app.
library;

export 'src/codec.dart';
export 'src/directory.dart';
export 'src/doc_store.dart';
export 'src/edge_client.dart';
export 'src/edge_list.dart';
export 'src/transport.dart';
