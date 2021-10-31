"""add thread_hash_short to message

Revision ID: 000000000022
Revises: 000000000021
Create Date: 2021-10-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '000000000022'
down_revision = '000000000021'
branch_labels = None
depends_on = None


def upgrade():
    # import os, sys
    # sys.path.append(os.path.abspath(os.path.join(__file__, "../../../..")))
    # from database.alembic_utils import post_alembic_write
    # post_alembic_write(revision)

    with op.batch_alter_table("message") as batch_op:
        batch_op.add_column(sa.Column('regenerate_popup_html', sa.Boolean))

    op.execute(
        '''
        UPDATE message
        SET regenerate_popup_html=0
        '''
    )


def downgrade():
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_column('regenerate_popup_html')
