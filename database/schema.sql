-- POSTEKS AI: schema only, no customer data. Alembic migrations are canonical.
PRAGMA foreign_keys=ON;

CREATE TABLE ai_usage (
	id INTEGER NOT NULL,
	model VARCHAR(100) NOT NULL,
	input_tokens INTEGER NOT NULL,
	output_tokens INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE app_settings (
	"key" VARCHAR(100) NOT NULL,
	value JSON NOT NULL,
	PRIMARY KEY ("key")
);

CREATE TABLE audit_events (
	id INTEGER NOT NULL,
	actor VARCHAR(160) NOT NULL,
	action VARCHAR(80) NOT NULL,
	entity_type VARCHAR(80) NOT NULL,
	entity_id VARCHAR(80) NOT NULL,
	detail JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE auth_sessions (
	id INTEGER NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	user_id INTEGER NOT NULL,
	expires_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE case_messages (
	id INTEGER NOT NULL,
	case_id INTEGER NOT NULL,
	body TEXT NOT NULL,
	kind VARCHAR(20) NOT NULL,
	author VARCHAR(160) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(case_id) REFERENCES service_cases (id)
);

CREATE TABLE chat_messages (
	id INTEGER NOT NULL,
	conversation_id VARCHAR(36) NOT NULL,
	role VARCHAR(20) NOT NULL,
	content TEXT NOT NULL,
	sources JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
);

CREATE TABLE conversation_states (
	conversation_id VARCHAR(36) NOT NULL,
	state JSON NOT NULL,
	revision INTEGER NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (conversation_id),
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
);

CREATE TABLE conversations (
	id VARCHAR(36) NOT NULL,
	owner_scope VARCHAR(100) NOT NULL,
	title VARCHAR(160) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE departments (
	id INTEGER NOT NULL,
	name VARCHAR(160) NOT NULL,
	responsibility VARCHAR(1000) NOT NULL,
	email VARCHAR(320) NOT NULL,
	backup_assignee VARCHAR(160) NOT NULL,
	active BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
);

CREATE TABLE document_pages (
	id INTEGER NOT NULL,
	document_id INTEGER NOT NULL,
	page INTEGER NOT NULL,
	text TEXT NOT NULL,
	layout JSON NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (document_id, page),
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE TABLE documents (
	id INTEGER NOT NULL,
	title VARCHAR(500) NOT NULL,
	filename VARCHAR(500) NOT NULL,
	storage_name VARCHAR(100) NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	mime_type VARCHAR(120) NOT NULL,
	size_bytes INTEGER NOT NULL,
	category VARCHAR(100) NOT NULL,
	models JSON NOT NULL,
	visibility VARCHAR(20) NOT NULL,
	status VARCHAR(30) NOT NULL,
	pages INTEGER NOT NULL,
	chunks INTEGER NOT NULL,
	version INTEGER NOT NULL,
	indexing_details JSON NOT NULL,
	error TEXT,
	deleted BOOLEAN NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (storage_name)
);

CREATE TABLE equipment (
	id INTEGER NOT NULL,
	serial_number VARCHAR(240) NOT NULL,
	normalized_serial VARCHAR(240) NOT NULL,
	model VARCHAR(240) NOT NULL,
	project VARCHAR(240) NOT NULL,
	name VARCHAR(500) NOT NULL,
	shipped_at VARCHAR(80),
	customer VARCHAR(240),
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE escalations (
	id INTEGER NOT NULL,
	conversation_id VARCHAR(36) NOT NULL,
	case_id INTEGER NOT NULL,
	task_id INTEGER NOT NULL,
	status VARCHAR(30) NOT NULL,
	reason TEXT NOT NULL,
	summary TEXT NOT NULL,
	known_facts JSON NOT NULL,
	missing_information JSON NOT NULL,
	attempted_actions JSON NOT NULL,
	document_sources JSON NOT NULL,
	attachments JSON NOT NULL,
	requested_action VARCHAR(40) NOT NULL,
	department VARCHAR(160) NOT NULL,
	priority VARCHAR(16) NOT NULL,
	assignee VARCHAR(160),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE,
	FOREIGN KEY(case_id) REFERENCES service_cases (id),
	FOREIGN KEY(task_id) REFERENCES internal_tasks (id)
);

CREATE TABLE guest_sessions (
	id INTEGER NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	expires_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE inbox_messages (
	id INTEGER NOT NULL,
	message_id VARCHAR(998) NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	case_id INTEGER,
	subject VARCHAR(998) NOT NULL,
	sender VARCHAR(320) NOT NULL,
	sender_name VARCHAR(240) NOT NULL,
	recipients JSON NOT NULL,
	body TEXT NOT NULL,
	"references" JSON NOT NULL,
	attachments JSON NOT NULL,
	raw_mime BLOB NOT NULL,
	source VARCHAR(30) NOT NULL,
	sent_at DATETIME,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (message_id),
	UNIQUE (content_hash),
	FOREIGN KEY(case_id) REFERENCES service_cases (id)
);

CREATE TABLE internal_tasks (
	id INTEGER NOT NULL,
	case_id INTEGER NOT NULL,
	title VARCHAR(400) NOT NULL,
	department VARCHAR(160) NOT NULL,
	assignee VARCHAR(160) NOT NULL,
	status VARCHAR(24) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(case_id) REFERENCES service_cases (id)
);

CREATE TABLE knowledge_chunks (
	id INTEGER NOT NULL,
	document_id INTEGER NOT NULL,
	page INTEGER NOT NULL,
	text TEXT NOT NULL,
	models JSON NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE TABLE mail_approvals (
	id INTEGER NOT NULL,
	inbox_id INTEGER NOT NULL,
	case_id INTEGER,
	subject VARCHAR(998) NOT NULL,
	recipient VARCHAR(320) NOT NULL,
	body TEXT NOT NULL,
	status VARCHAR(24) NOT NULL,
	created_by VARCHAR(160) NOT NULL,
	approved_by VARCHAR(160),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(inbox_id) REFERENCES inbox_messages (id),
	FOREIGN KEY(case_id) REFERENCES service_cases (id)
);

CREATE TABLE mail_attachments (
	id INTEGER NOT NULL,
	inbox_id INTEGER NOT NULL,
	filename VARCHAR(240) NOT NULL,
	content_type VARCHAR(160) NOT NULL,
	size BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	storage_path TEXT,
	status VARCHAR(24) NOT NULL,
	reason VARCHAR(240),
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_mail_attachment UNIQUE (inbox_id, sha256, filename),
	FOREIGN KEY(inbox_id) REFERENCES inbox_messages (id)
);

CREATE TABLE mail_folder_states (
	id INTEGER NOT NULL,
	account_key VARCHAR(64) NOT NULL,
	folder VARCHAR(512) NOT NULL,
	kind VARCHAR(16) NOT NULL,
	uidvalidity VARCHAR(80),
	last_uid BIGINT NOT NULL,
	new_initialized BOOLEAN NOT NULL,
	initial_upper_uid BIGINT,
	initial_since VARCHAR(10),
	history_last_uid BIGINT NOT NULL,
	history_since VARCHAR(10),
	history_days INTEGER,
	history_completed BOOLEAN NOT NULL,
	last_sync DATETIME,
	error TEXT,
	PRIMARY KEY (id),
	CONSTRAINT uq_mail_folder_account UNIQUE (account_key, folder)
);

CREATE TABLE mail_remote_messages (
	id INTEGER NOT NULL,
	account_key VARCHAR(64) NOT NULL,
	folder VARCHAR(512) NOT NULL,
	uidvalidity VARCHAR(80) NOT NULL,
	uid BIGINT NOT NULL,
	inbox_id INTEGER,
	status VARCHAR(24) NOT NULL,
	direction VARCHAR(16) NOT NULL,
	error_code VARCHAR(80),
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_mail_remote_uid UNIQUE (account_key, folder, uidvalidity, uid),
	FOREIGN KEY(inbox_id) REFERENCES inbox_messages (id)
);

CREATE TABLE mail_solution_origins (
	id INTEGER NOT NULL,
	origin_key VARCHAR(64) NOT NULL,
	solution_id INTEGER NOT NULL,
	case_id INTEGER NOT NULL,
	inbox_id INTEGER NOT NULL,
	equipment_model VARCHAR(120) NOT NULL,
	evidence JSON NOT NULL,
	created_by VARCHAR(160) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (origin_key),
	UNIQUE (solution_id),
	FOREIGN KEY(solution_id) REFERENCES verified_solutions (id),
	FOREIGN KEY(case_id) REFERENCES service_cases (id),
	FOREIGN KEY(inbox_id) REFERENCES inbox_messages (id)
);

CREATE TABLE mail_sync_controls (
	account_key VARCHAR(64) NOT NULL,
	background_enabled BOOLEAN NOT NULL,
	poll_seconds INTEGER NOT NULL,
	lease_owner VARCHAR(80),
	lease_until DATETIME,
	last_heartbeat DATETIME,
	last_connection DATETIME,
	connection_error VARCHAR(80),
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (account_key)
);

CREATE TABLE mail_sync_runs (
	id INTEGER NOT NULL,
	account_key VARCHAR(64) NOT NULL,
	kind VARCHAR(16) NOT NULL,
	status VARCHAR(24) NOT NULL,
	days INTEGER NOT NULL,
	batch_size INTEGER NOT NULL,
	imported INTEGER NOT NULL,
	duplicates INTEGER NOT NULL,
	fetched INTEGER NOT NULL,
	errors INTEGER NOT NULL,
	progress JSON NOT NULL,
	has_more BOOLEAN NOT NULL,
	stop_requested BOOLEAN NOT NULL,
	error_code VARCHAR(80),
	error TEXT,
	actor VARCHAR(160) NOT NULL,
	created_at DATETIME NOT NULL,
	started_at DATETIME,
	finished_at DATETIME,
	PRIMARY KEY (id)
);

CREATE TABLE service_cases (
	id INTEGER NOT NULL,
	service_number VARCHAR(80),
	title VARCHAR(240) NOT NULL,
	company VARCHAR(240) NOT NULL,
	contact VARCHAR(240) NOT NULL,
	email VARCHAR(320) NOT NULL,
	equipment_model VARCHAR(120) NOT NULL,
	serial_number VARCHAR(160) NOT NULL,
	status VARCHAR(24) NOT NULL,
	priority VARCHAR(16) NOT NULL,
	source VARCHAR(30) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (service_number)
);

CREATE TABLE users (
	id INTEGER NOT NULL,
	username VARCHAR(100) NOT NULL,
	password_hash TEXT NOT NULL,
	role VARCHAR(30) NOT NULL,
	active BOOLEAN NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE verified_solutions (
	id INTEGER NOT NULL,
	title VARCHAR(240) NOT NULL,
	equipment_model VARCHAR(120) NOT NULL,
	symptoms TEXT NOT NULL,
	actions TEXT NOT NULL,
	limitations TEXT NOT NULL,
	source TEXT NOT NULL,
	customer_confirmed BOOLEAN NOT NULL,
	status VARCHAR(24) NOT NULL,
	visibility VARCHAR(20) NOT NULL,
	verified_by VARCHAR(160),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_ai_usage_created_at ON ai_usage (created_at);

CREATE UNIQUE INDEX ix_auth_sessions_token_hash ON auth_sessions (token_hash);

CREATE INDEX ix_case_messages_case_id ON case_messages (case_id);

CREATE INDEX ix_chat_messages_conversation_id ON chat_messages (conversation_id);

CREATE INDEX ix_conversations_owner_scope ON conversations (owner_scope);

CREATE INDEX ix_document_pages_document_id ON document_pages (document_id);

CREATE INDEX ix_documents_sha256 ON documents (sha256);

CREATE UNIQUE INDEX ix_equipment_normalized_serial ON equipment (normalized_serial);

CREATE INDEX ix_escalations_case_id ON escalations (case_id);

CREATE UNIQUE INDEX ix_escalations_conversation_id ON escalations (conversation_id);

CREATE INDEX ix_escalations_status ON escalations (status);

CREATE INDEX ix_escalations_task_id ON escalations (task_id);

CREATE UNIQUE INDEX ix_guest_sessions_token_hash ON guest_sessions (token_hash);

CREATE INDEX ix_inbox_messages_case_id ON inbox_messages (case_id);

CREATE INDEX ix_internal_tasks_case_id ON internal_tasks (case_id);

CREATE INDEX ix_knowledge_chunks_document_id ON knowledge_chunks (document_id);

CREATE INDEX ix_mail_approvals_status ON mail_approvals (status);

CREATE INDEX ix_mail_attachments_inbox_id ON mail_attachments (inbox_id);

CREATE INDEX ix_mail_folder_states_account_key ON mail_folder_states (account_key);

CREATE INDEX ix_mail_remote_messages_account_key ON mail_remote_messages (account_key);

CREATE INDEX ix_mail_remote_messages_inbox_id ON mail_remote_messages (inbox_id);

CREATE INDEX ix_mail_sync_runs_account_key ON mail_sync_runs (account_key);

CREATE INDEX ix_mail_sync_runs_status ON mail_sync_runs (status);

CREATE INDEX ix_service_cases_status ON service_cases (status);

CREATE UNIQUE INDEX ix_users_username ON users (username);

CREATE INDEX ix_verified_solutions_equipment_model ON verified_solutions (equipment_model);

CREATE INDEX ix_verified_solutions_status ON verified_solutions (status);
