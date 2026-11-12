"""The indexer's document store: Firestore's data model and client API on one SQLite file.

The indexer writes documents here instead of to Firestore; the publisher tails the store's change
log and publishes the documents to R2. `import docstore as firestore` gives the names the indexer's
modules used from firebase_admin.firestore and google.cloud.firestore_v1.
"""
from .client import (AlreadyExists, Client, CollectionReference, DocumentReference, DocumentSnapshot,
                     FieldFilter, NotFound, Query, Transaction, WriteBatch, transactional)
from .fields import (DELETE_FIELD, SERVER_TIMESTAMP, ArrayRemove, ArrayUnion, FieldPath, Increment)
from .store import Store

__all__ = [
    "AlreadyExists", "ArrayRemove", "ArrayUnion", "Client", "CollectionReference", "DELETE_FIELD",
    "DocumentReference", "DocumentSnapshot", "FieldFilter", "FieldPath", "Increment", "NotFound",
    "Query", "SERVER_TIMESTAMP", "Store", "Transaction", "WriteBatch", "transactional",
]
