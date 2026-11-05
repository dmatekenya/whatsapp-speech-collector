"""provider-neutral columns for Twilio and Meta

outbound_messages.twilio_sid -> provider_message_id, plus channel;
recordings.twilio_media_deleted -> provider_media_deleted, plus is_voice_note
and forwarded. Columns are renamed in place, so existing values are kept;
existing outbound rows were all sent via Twilio.

Revision ID: b7c3e9a41d20
Revises: 98754c812402
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c3e9a41d20'
down_revision: Union[str, Sequence[str], None] = '98754c812402'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # =================
    # OUTBOUND MESSAGES
    # =================
    op.add_column(
        'outbound_messages', sa.Column('channel', sa.Text(), server_default='twilio', nullable=False)
    )
    op.create_check_constraint(
        'ck_outbound_messages_channel', 'outbound_messages', "channel IN ('twilio', 'meta')"
    )
    op.alter_column('outbound_messages', 'twilio_sid', new_column_name='provider_message_id')
    # IDs are unique per provider, not across them.
    op.drop_constraint('outbound_messages_twilio_sid_key', 'outbound_messages', type_='unique')
    op.create_unique_constraint(
        'uq_outbound_messages_channel_provider_message_id',
        'outbound_messages',
        ['channel', 'provider_message_id'],
    )

    # ==========
    # RECORDINGS
    # ==========
    op.alter_column('recordings', 'twilio_media_deleted', new_column_name='provider_media_deleted')
    op.add_column('recordings', sa.Column('is_voice_note', sa.Boolean(), nullable=True))
    op.add_column('recordings', sa.Column('forwarded', sa.Text(), nullable=True))
    op.create_check_constraint(
        'ck_recordings_forwarded',
        'recordings',
        "forwarded IN ('no', 'forwarded', 'frequently_forwarded')",
    )


def downgrade() -> None:
    """Downgrade schema. Meta rows lose their channel, so only downgrade before Meta is used."""
    op.drop_constraint('ck_recordings_forwarded', 'recordings', type_='check')
    op.drop_column('recordings', 'forwarded')
    op.drop_column('recordings', 'is_voice_note')
    op.alter_column('recordings', 'provider_media_deleted', new_column_name='twilio_media_deleted')

    op.drop_constraint(
        'uq_outbound_messages_channel_provider_message_id', 'outbound_messages', type_='unique'
    )
    op.alter_column('outbound_messages', 'provider_message_id', new_column_name='twilio_sid')
    op.create_unique_constraint('outbound_messages_twilio_sid_key', 'outbound_messages', ['twilio_sid'])
    op.drop_constraint('ck_outbound_messages_channel', 'outbound_messages', type_='check')
    op.drop_column('outbound_messages', 'channel')
