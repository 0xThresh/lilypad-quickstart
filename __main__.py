import pulumi
import pulumi_aws as aws
import json

# Get the Pulumi config to access DataDog API key and HF token (if applicable)
config = pulumi.Config()
# You need a separate account for each GPU you want to set up on the network; for now just using single GPU instance
web3_private_key = config.require("Web3PrivateKey")
alchemy_api_key = config.require("AlchemyAPIKey")

# Create a VPC
vpc = aws.ec2.Vpc("lilypad-vpc",
    cidr_block="10.7.0.0/16",
    enable_dns_hostnames=True,
    enable_dns_support=True,
    tags={
        "Name": "lilypad-vpc"
    }
)

# Create public subnet
public_subnet = aws.ec2.Subnet("public-subnet",
    vpc_id=vpc.id,
    cidr_block="10.7.1.0/24",
    availability_zone="us-west-2a",
    map_public_ip_on_launch=True,
    tags={
        "Name": "lilypad-public-subnet"
    }
)

# Create private subnet
private_subnet = aws.ec2.Subnet("private-subnet",
    vpc_id=vpc.id,
    cidr_block="10.7.2.0/24",
    availability_zone="us-west-2b",
    tags={
        "Name": "lilypad-private-subnet"
    }
)

# Create Internet Gateway
igw = aws.ec2.InternetGateway("lilypad-igw",
    vpc_id=vpc.id,
    tags={
        "Name": "lilypad-igw"
    }
)

# Create public route table
public_rt = aws.ec2.RouteTable("public-rt",
    vpc_id=vpc.id,
    routes=[{
        "cidr_block": "0.0.0.0/0",
        "gateway_id": igw.id
    }],
    tags={
        "Name": "lilypad-public-rt"
    }
)

# Associate public subnet with public route table
public_rt_assoc = aws.ec2.RouteTableAssociation("public-rt-assoc",
    subnet_id=public_subnet.id,
    route_table_id=public_rt.id
)

ssm_role = aws.iam.Role("ssm-role",
    assume_role_policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Action": "sts:AssumeRole",
            "Principal": {
                "Service": "ec2.amazonaws.com"
            },
            "Effect": "Allow",
        }]
    }),
    tags={
        "Name": "lilypad-ssm-role"
    }
)

# Attach the SSM policy to the role
role_policy_attachment = aws.iam.RolePolicyAttachment("ssm-policy",
    role=ssm_role.name,
    policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
)

# Create the instance profile
instance_profile = aws.iam.InstanceProfile("ssm-instance-profile",
    role=ssm_role.name,
    tags={
        "Name": "lilypad-instance-profile"
    }
)

# Create EC2 instance in the public subnet
ec2_instance = aws.ec2.Instance("lilypad-instance",
    instance_type="g5.2xlarge",
    ami="ami-081f526a977142913",
    subnet_id=public_subnet.id,
    iam_instance_profile=instance_profile.name,
    user_data=f"""#!/bin/bash
# Get secrets, sourced from Pulumi
export WEB3_PRIVATE_KEY={web3_private_key}
export ALCHEMY_API_KEY={alchemy_api_key}

# NVIDIA container toolkit should already be installed, but Docker needs configured
sudo nvidia-ctk runtime configure --runtime=docker --set-as-default
sudo systemctl restart docker

# Download the latest version of Kubo (go-ipfs):
wget https://dist.ipfs.tech/kubo/v0.30.0/kubo_v0.30.0_linux-amd64.tar.gz
# Extract the archive:
tar -xvzf kubo_v0.30.0_linux-amd64.tar.gz
# Change to the Kubo directory:
cd kubo
# Run the installation script:
sudo bash install.sh
# Now remove the downloaded archive file:
cd ..
rm kubo_v0.30.0_linux-amd64.tar.gz
# Create a new ipfs directory
sudo mkdir -p /app/data/ipfs
# Set IPFS_PATH environment variable
echo 'export IPFS_PATH=/app/data/ipfs' >> ~/.bashrc && source ~/.bashrc
# Change ownership of the ipfs directory to your user
sudo chown -R $USER:$USER /app/data/ipfs
# Initialize ipfs node
ipfs init 
# Start the ipfs node
ipfs daemon &

# Install Bacalhau
cd /tmp
wget https://github.com/bacalhau-project/bacalhau/releases/download/v1.3.2/bacalhau_v1.3.2_linux_amd64.tar.gz
tar xfv bacalhau_v1.3.2_linux_amd64.tar.gz
sudo mv bacalhau /usr/bin/bacalhau
sudo chown -R $USER /app/data

# Install Lilypad - remove logic to detect OSARCH and OSNAME since it messes up Pulumi userdata
export OSARCH="amd64"
export OSNAME="linux"
# Remove existing lilypad installation if it exists
sudo rm -f /usr/local/bin/lilypad
# Download the latest production build
curl https://api.github.com/repos/lilypad-tech/lilypad/releases/latest | grep "browser_download_url.*lilypad-$OSNAME-$OSARCH-gpu" | cut -d : -f 2,3 | tr -d \" | wget -qi - -O lilypad
# Make Lilypad executable and install it
chmod +x lilypad
sudo mv lilypad /usr/local/bin/lilypad

# Create env file
sudo mkdir -p /app/lilypad
sudo touch /app/lilypad/resource-provider-gpu.env
sudo echo $WEB3_PRIVATE_KEY > /app/lilypad/resource-provider-gpu.env

# Set up Arbitrum RPC connection
export RPC=wss://arb-sepolia.g.alchemy.com/v2/$ALCHEMY_API_KEY

# Set up the Bacalhau unit 
cat << EOT > /etc/systemd/system/bacalhau.service
[Unit]
Description=Lilypad V2 Bacalhau
After=network-online.target
Wants=network-online.target systemd-networkd-wait-online.service
			
[Service]
Environment="LOG_TYPE=json"
Environment="LOG_LEVEL=debug"
Environment="HOME=/app/lilypad"
Environment="BACALHAU_SERVE_IPFS_PATH=/app/data/ipfs"
Restart=always
RestartSec=5s
ExecStart=/usr/bin/bacalhau serve --node-type compute,requester --peer none --private-internal-ipfs=false --ipfs-connect "/ip4/127.0.0.1/tcp/5001"

[Install]
WantedBy=multi-user.target 
EOT

# Set up the Lilypad Resource Provider unit
cat << EOT > /etc/systemd/system/lilypad-resource-provider.service
[Unit]
Description=Lilypad V2 Resource Provider GPU
After=network-online.target
Wants=network-online.target systemd-networkd-wait-online.service

[Service]
Environment="LOG_TYPE=json"
Environment="LOG_LEVEL=debug"
Environment="HOME=/app/lilypad"
Environment="OFFER_GPU=1"
EnvironmentFile=/app/lilypad/resource-provider-gpu.env
Restart=always
RestartSec=5s
ExecStart=/usr/local/bin/lilypad resource-provider 

[Install]
WantedBy=multi-user.target
EOT

# Start Lilypad services
sudo systemctl daemon-reload
sudo systemctl enable bacalhau
sudo systemctl enable lilypad-resource-provider
sudo systemctl start bacalhau
sleep 30
sudo systemctl start lilypad-resource-provider

# Start the Lilypad Resource Provider on Docker
#docker run -d --gpus all -e WEB3_PRIVATE_KEY=$WEB3_PRIVATE_KEY -e WEB3_RPC_URL=$RPC --restart always ghcr.io/lilypad-tech/resource-provider:latest
""",
    root_block_device={
        "volume_size": 120,
        "volume_type": "gp3",
        "delete_on_termination": True,
    },
    tags={
        "Name": "lilypad"
    }
)

# Export the important values
pulumi.export("vpc_id", vpc.id)
pulumi.export("public_subnet_id", public_subnet.id)
pulumi.export("private_subnet_id", private_subnet.id)
pulumi.export("instance_id", ec2_instance.id)
pulumi.export("public_ip", ec2_instance.public_ip)
