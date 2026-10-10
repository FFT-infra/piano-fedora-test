.PHONY: test plan clean

test:
	python3 tests/check_sources.py
	python3 tests/check_product.py
	python3 tests/check_links.py
	python3 tests/check_document_scope.py
	python3 tests/check_rootfs_plan.py
	python3 tests/check_f2fs_image.py
	python3 tests/check_f2fs_metadata.py
	python3 tests/check_first_partition.py
	python3 tests/check_esp_image.py
	python3 tests/check_patches.py
	python3 tests/check_initramfs.py
	python3 tests/check_rootfs_policy.py
	python3 tests/check_stage_payload.py
	python3 tests/check_image_workflow.py
	python3 tests/check_rootfs_finalize.py

RELEASEVER ?= 44
plan:
	python3 scripts/build-fedora-rootfs.py --plan --releasever $(RELEASEVER)

clean:
	rm -rf build
